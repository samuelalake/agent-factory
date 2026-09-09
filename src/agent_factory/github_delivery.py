"""Publish runner-produced evidence into Builder's canonical pull-request body."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


DELIVERY_START = "<!-- agent-factory:builder-delivery:start -->"
DELIVERY_END = "<!-- agent-factory:builder-delivery:end -->"
DELIVERY_STATUS = "<!-- agent-factory:builder-delivery-status:{status} -->"
DELIVERY_HEAD = "<!-- agent-factory:builder-delivery-head:{head} -->"
VALID_STATUSES = {"pending", "ready", "failed"}
MAX_NATIVE_ATTACHMENTS = 50
MAX_NATIVE_ATTACHMENT_BYTES = 10 * 1024 * 1024
SUPPORTED_ATTACHMENT_SUFFIXES = {
    ".gif", ".jpeg", ".jpg", ".mov", ".mp4", ".png", ".webm", ".webp",
}


def _gh(
    args: list[str], *, stdin: str | None = None, token: str | None = None
) -> str:
    environment = None
    if token:
        environment = os.environ.copy()
        environment["GH_TOKEN"] = token
    result = subprocess.run(
        ["gh", *args], input=stdin, text=True, capture_output=True, env=environment
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def _validate_attachments(attachments: tuple[Path, ...]) -> None:
    """Fail before publication if any attachment is invalid or non-portable."""
    if len(attachments) > MAX_NATIVE_ATTACHMENTS:
        raise ValueError(
            f"Builder delivery has {len(attachments)} native attachments; "
            f"maximum is {MAX_NATIVE_ATTACHMENTS}"
        )
    normalized = [path.resolve() for path in attachments]
    if len(set(normalized)) != len(normalized):
        raise ValueError("Builder delivery cannot attach the same file twice")
    for path in attachments:
        if not path.is_file():
            raise ValueError(f"Builder media attachment is not a regular file: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise ValueError(f"Builder media attachment is empty: {path}")
        if size > MAX_NATIVE_ATTACHMENT_BYTES:
            raise ValueError(
                f"Builder media attachment exceeds the portable 10 MB limit: {path}"
            )
        if path.suffix.lower() not in SUPPORTED_ATTACHMENT_SUFFIXES:
            raise ValueError(f"unsupported Builder media attachment: {path}")
        content_type, _ = mimetypes.guess_type(path.name)
        if not content_type or not content_type.startswith(("image/", "video/")):
            raise ValueError(f"unsupported Builder media attachment: {path}")


def _stage_native_attachments(
    repo: str,
    pr: str,
    attachments: tuple[Path, ...],
    token: str,
) -> dict[str, str]:
    """Upload with a disposable Builder comment and return durable asset URLs."""
    marker = f"<!-- agent-factory:media-staging:{uuid.uuid4().hex} -->"
    references = [
        f"![]({path})" if mimetypes.guess_type(path.name)[0].startswith("video/")
        else f"![{path.name}]({path})"
        for path in attachments
    ]
    staged_body = "\n\n".join([marker, *references])
    upload_error: RuntimeError | None = None
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as body_file:
        body_file.write(staged_body)
        body_file.flush()
        command = [
            "pr", "comment", pr, "--repo", repo, "--body-file", body_file.name,
        ]
        for attachment in attachments:
            command.extend(["--attach", str(attachment)])
        try:
            _gh(command, token=token)
        except RuntimeError as error:
            # The CLI may create a partially attached comment before returning
            # nonzero, so cleanup must not depend on command success.
            upload_error = error

    pages = json.loads(_gh([
        "api", f"repos/{repo}/issues/{pr}/comments", "--paginate", "--slurp",
    ], token=token))
    comments = [comment for page in pages for comment in page]
    staged = [comment for comment in comments if marker in str(comment.get("body") or "")]
    if len(staged) > 1:
        for duplicate in staged:
            duplicate_id = str(duplicate.get("id") or "")
            if duplicate_id:
                _gh([
                    "api", f"repos/{repo}/issues/comments/{duplicate_id}",
                    "-X", "DELETE",
                ], token=token)
        raise RuntimeError("GitHub created duplicate Builder media staging comments")
    if not staged:
        if upload_error:
            raise upload_error
        raise RuntimeError("GitHub created no Builder media staging comment")

    comment = staged[0]
    comment_id = str(comment.get("id") or "")
    body = str(comment.get("body") or "")
    if not comment_id:
        raise RuntimeError("GitHub media staging comment has no authenticated id")
    try:
        if upload_error:
            raise upload_error
        urls = re.findall(r"https://github\.com/user-attachments/[^\s)]+", body)
        if len(urls) != len(attachments):
            raise RuntimeError("GitHub CLI did not publish every Builder media attachment")
        if any(str(path) in body for path in attachments):
            raise RuntimeError("GitHub CLI did not rewrite every Builder media attachment")
        return {str(path): url for path, url in zip(attachments, urls)}
    finally:
        _gh(
            ["api", f"repos/{repo}/issues/comments/{comment_id}", "-X", "DELETE"],
            token=token,
        )


def _rewrite_attachment_references(content: str, urls: dict[str, str]) -> str:
    """Replace only exact Markdown destinations, preserving unrelated path text."""
    rewritten = content
    for local_path, url in urls.items():
        path = Path(local_path)
        destination = f"]({local_path})"
        if destination not in rewritten:
            raise ValueError(f"Builder delivery does not reference attachment: {path.name}")
        if path.suffix.lower() in {".mov", ".mp4", ".webm"}:
            video_reference = f"![]({local_path})"
            if video_reference not in rewritten:
                raise ValueError(
                    f"Builder video must use a standalone empty-alt reference: {path.name}"
                )
            rewritten = rewritten.replace(video_reference, url)
        else:
            rewritten = rewritten.replace(destination, f"]({url})")
    if any(f"]({local_path})" in rewritten for local_path in urls):
        raise RuntimeError("Builder delivery retained a local media reference")
    if any(url not in rewritten for url in urls.values()):
        raise RuntimeError("Builder delivery omitted a staged media attachment")
    return rewritten


def format_delivery(status: str, content: str, *, head: str | None = None) -> str:
    if status not in VALID_STATUSES:
        raise ValueError(f"unsupported Builder delivery status: {status}")
    clean = content.strip()
    markers = [DELIVERY_STATUS.format(status=status)]
    if head:
        markers.append(DELIVERY_HEAD.format(head=head))
    return "\n".join([
        DELIVERY_START,
        *markers,
        "",
        "## Delivery",
        "",
        clean or "_No delivery evidence was supplied._",
        DELIVERY_END,
    ])


def pending_delivery() -> str:
    return format_delivery(
        "pending",
        "_Current-head repository verification, documentation, and media are pending._",
    )


def delivery_head(body: str) -> str | None:
    match = re.search(r"<!-- agent-factory:builder-delivery-head:([0-9a-f]{40}) -->", body)
    return match.group(1) if match else None


def delivery_status(body: str, *, expected_head: str | None = None) -> str | None:
    for status in VALID_STATUSES:
        if DELIVERY_STATUS.format(status=status) in body:
            if status != "pending" and expected_head and delivery_head(body) != expected_head:
                return "stale"
            return status
    return None


def replace_delivery(body: str, delivery: str) -> str:
    start = body.find(DELIVERY_START)
    end = body.find(DELIVERY_END)
    if start < 0 or end < start:
        raise ValueError("pull request body has no canonical Builder delivery section")
    end += len(DELIVERY_END)
    return body[:start] + delivery + body[end:]


def wait_for_delivery(
    repo: str,
    pr: str,
    head: str,
    *,
    timeout_seconds: int,
    poll_seconds: int = 15,
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        meta = json.loads(_gh([
            "pr", "view", pr, "--repo", repo, "--json", "headRefOid,body",
        ]))
        current_head = str(meta.get("headRefOid") or "")
        body = str(meta.get("body") or "")
        if current_head != head:
            return "stale", body
        status = delivery_status(body, expected_head=head)
        if status != "pending" or time.monotonic() >= deadline:
            return status or "missing", body
        time.sleep(poll_seconds)


def publish(
    repo: str,
    pr: str,
    head: str,
    status: str,
    content: str,
    attachments: tuple[Path, ...] = (),
) -> None:
    meta = json.loads(_gh([
        "pr", "view", pr, "--repo", repo, "--json", "headRefOid,body",
    ]))
    current_head = str(meta.get("headRefOid") or "")
    if current_head != head:
        raise RuntimeError(
            f"refusing stale Builder evidence for {head[:7]}; current head is {current_head[:7]}"
        )
    if attachments:
        _validate_attachments(attachments)
        media_token = os.environ.get("AGENT_FACTORY_MEDIA_UPLOAD_TOKEN", "").strip()
        if not media_token:
            raise RuntimeError(
                "AGENT_FACTORY_MEDIA_UPLOAD_TOKEN is required for native Builder media"
            )
        urls = _stage_native_attachments(repo, pr, attachments, media_token)
        content = _rewrite_attachment_references(content, urls)
        # The slow upload never mutates the PR body. Refresh the exact head and
        # latest body before Builder performs the canonical write.
        meta = json.loads(_gh([
            "pr", "view", pr, "--repo", repo, "--json", "headRefOid,body",
        ]))
        current_head = str(meta.get("headRefOid") or "")
        if current_head != head:
            raise RuntimeError(
                f"refusing stale Builder evidence for {head[:7]}; current head is {current_head[:7]}"
            )
    delivery = format_delivery(status, content, head=head)
    updated = replace_delivery(str(meta.get("body") or ""), delivery)
    _gh(
        ["api", f"repos/{repo}/pulls/{pr}", "-X", "PATCH", "--input", "-"],
        stdin=json.dumps({"body": updated}),
    )
    published = json.loads(_gh([
        "pr", "view", pr, "--repo", repo, "--json", "headRefOid,body",
    ]))
    published_head = str(published.get("headRefOid") or "")
    published_body = str(published.get("body") or "")
    if published_head != head or delivery_head(published_body) != head:
        raise RuntimeError(
            f"Builder evidence publication raced a new head; expected {head[:7]}, "
            f"found {published_head[:7]}"
        )
    if attachments:
        published_start = published_body.find(DELIVERY_START)
        published_end = published_body.find(DELIVERY_END, published_start)
        published_delivery = published_body[published_start:published_end]
        if any(url not in published_delivery for url in urls.values()):
            raise RuntimeError("canonical delivery omitted native Builder media")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--status", required=True, choices=sorted(VALID_STATUSES))
    parser.add_argument("--body-file", type=Path, required=True)
    parser.add_argument("--attach", type=Path, action="append", default=[])
    args = parser.parse_args()
    publish(
        args.repo,
        args.pr,
        args.head,
        args.status,
        args.body_file.read_text(encoding="utf-8"),
        tuple(args.attach),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
