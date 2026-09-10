"""Publish runner-produced evidence into Builder's canonical pull-request body."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .config import load_config


DELIVERY_START = "<!-- agent-factory:builder-delivery:start -->"
DELIVERY_END = "<!-- agent-factory:builder-delivery:end -->"
DELIVERY_STATUS = "<!-- agent-factory:builder-delivery-status:{status} -->"
DELIVERY_HEAD = "<!-- agent-factory:builder-delivery-head:{head} -->"
DELIVERY_EVIDENCE = "<!-- agent-factory:builder-evidence:{payload} -->"
DELIVERY_PROVENANCE = "<!-- agent-factory:builder-evidence-provenance -->"
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


def _evidence_manifest(
    repo: str,
    pr: str,
    head: str,
    attachments: tuple[Path, ...],
    urls: dict[str, str],
    content: str = "",
) -> str:
    entries = []
    for path in attachments:
        content_type, _ = mimetypes.guess_type(path.name)
        role, scope = _evidence_identity(content, urls[str(path)], path)
        entries.append({
            "url": urls[str(path)],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "content_type": content_type,
            "role": role,
            "scope": scope,
        })
    raw = json.dumps(
        {
            "version": 2,
            "repo": repo,
            "pr": int(pr),
            "head": head,
            "attachments": entries,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return DELIVERY_EVIDENCE.format(payload=payload)


def _evidence_identity(content: str, url: str, path: Path) -> tuple[str, str]:
    """Derive a stable semantic role and scope from Builder's visible label."""
    match = re.search(rf"!\[([^]]*)\]\({re.escape(url)}\)", content)
    label = (match.group(1) if match else path.stem).strip()
    lowered = label.lower()
    content_type, _ = mimetypes.guess_type(path.name)
    if content_type and content_type.startswith("video/"):
        role = "recording"
    elif re.search(r"\b(origami|reference|expected)\b", lowered):
        role = "reference"
    elif re.search(r"\b(diff|difference)\b", lowered):
        role = "diff"
    elif re.search(r"\b(swami|render|actual|current)\b", lowered):
        role = "render"
    else:
        role = "image" if content_type and content_type.startswith("image/") else "media"
    scope = re.sub(
        r"\b(swami|origami|reference|expected|diff(?:erence)?|render|actual|current|recording)\b",
        " ",
        label,
        flags=re.IGNORECASE,
    )
    scope = re.sub(r"[^A-Za-z0-9]+", "-", scope).strip("-").lower()
    return role, scope or "delivery"


def delivery_evidence_manifest(
    body: str,
    *,
    expected_repo: str,
    expected_pr: int,
    expected_head: str,
) -> dict[str, dict[str, str]] | None:
    """Decode one publisher-produced native-attachment allowlist."""
    payloads = re.findall(
        r"<!-- agent-factory:builder-evidence:([A-Za-z0-9_-]+) -->",
        body,
    )
    if len(payloads) != 1:
        return None
    try:
        payload = payloads[0]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        manifest = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") not in {1, 2}
        or manifest.get("repo") != expected_repo
        or manifest.get("pr") != expected_pr
        or manifest.get("head") != expected_head
        or not isinstance(manifest.get("attachments"), list)
    ):
        return None
    legacy_types = [
        item.get("content_type") if isinstance(item, dict) else None
        for item in manifest["attachments"]
    ]
    legacy_single_pattern = bool(
        manifest.get("version") == 1
        and len(legacy_types) in {3, 4}
        and all(str(value).startswith("image/") for value in legacy_types[:3])
        and (
            len(legacy_types) == 3
            or str(legacy_types[3]).startswith("video/")
        )
    )
    entries: dict[str, dict[str, str]] = {}
    for index, item in enumerate(manifest["attachments"]):
        if not isinstance(item, dict):
            return None
        url = item.get("url")
        digest = item.get("sha256")
        content_type = item.get("content_type")
        inferred_role, inferred_scope = _evidence_identity(
            body, str(url or ""), Path(f"attachment-{index}{mimetypes.guess_extension(str(content_type or '')) or ''}")
        )
        if legacy_single_pattern and inferred_role == "image":
            inferred_role = ("render", "reference", "diff")[index]
            inferred_scope = "delivery"
        role = item.get("role", inferred_role)
        scope = item.get("scope", inferred_scope)
        if (
            not isinstance(url, str)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(content_type, str)
            or not content_type.startswith(("image/", "video/"))
            or role not in {"render", "reference", "diff", "recording", "image", "media"}
            or not isinstance(scope, str)
            or not scope
            or url in entries
        ):
            return None
        entries[url] = {
            "sha256": digest,
            "content_type": content_type,
            "role": role,
            "scope": scope,
        }
    return entries


def evidence_manifests_match(
    left: dict[str, dict[str, str]], right: dict[str, dict[str, str]]
) -> bool:
    """Compare authenticated bytes while tolerating derived v1 identity metadata."""
    return (
        tuple(left) == tuple(right)
        and all(
            left[url].get("sha256") == right[url].get("sha256")
            and left[url].get("content_type") == right[url].get("content_type")
            for url in left
        )
    )


def authenticated_delivery_evidence(
    comments: object,
    *,
    expected_repo: str,
    expected_pr: int,
    expected_head: str,
    builder_app_login: str,
    expected_manifest: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]] | None:
    """Authenticate the body-selected manifest against configured Builder comments."""
    if not isinstance(comments, list):
        return None
    pages = comments if comments and isinstance(comments[0], list) else [comments]
    matched = False
    for page in pages:
        if not isinstance(page, list):
            return None
        for comment in page:
            if not isinstance(comment, dict):
                return None
            user = comment.get("user") or {}
            body = str(comment.get("body") or "")
            if (
                not isinstance(user, dict)
                or user.get("type") != "Bot"
                or user.get("login") != builder_app_login
                or DELIVERY_PROVENANCE not in body
            ):
                continue
            manifest = delivery_evidence_manifest(
                body,
                expected_repo=expected_repo,
                expected_pr=expected_pr,
                expected_head=expected_head,
            )
            if manifest is not None:
                matched = matched or evidence_manifests_match(manifest, expected_manifest)
    return expected_manifest if matched else None


def authenticated_delivery_history(
    comments: object,
    *,
    expected_repo: str,
    expected_pr: int,
    builder_app_login: str,
) -> tuple[dict[str, object], ...]:
    """Return Builder-authored evidence manifests for prior PR heads, oldest first."""
    if not isinstance(comments, list):
        return ()
    pages = comments if comments and isinstance(comments[0], list) else [comments]
    history: list[dict[str, object]] = []
    for page in pages:
        if not isinstance(page, list):
            return ()
        for comment in page:
            if not isinstance(comment, dict):
                return ()
            user = comment.get("user") or {}
            body = str(comment.get("body") or "")
            head_match = re.search(r"Authenticated media manifest for `([0-9a-f]{40})`", body)
            if not (
                isinstance(user, dict)
                and user.get("type") == "Bot"
                and user.get("login") == builder_app_login
                and DELIVERY_PROVENANCE in body
                and head_match
            ):
                continue
            head = head_match.group(1)
            manifest = delivery_evidence_manifest(
                body,
                expected_repo=expected_repo,
                expected_pr=expected_pr,
                expected_head=head,
            )
            if manifest is not None:
                history.append({
                    "head": head,
                    "attachments": manifest,
                    "comment_id": comment.get("id"),
                    "created_at": str(comment.get("created_at") or ""),
                })
    history.sort(key=lambda item: (str(item["created_at"]), int(item["comment_id"] or 0)))
    return tuple(history)


def _publish_evidence_provenance(
    repo: str,
    pr: str,
    head: str,
    manifest_marker: str,
    builder_app_login: str,
) -> None:
    """Upsert one Builder-authored provenance comment for this exact source head."""
    raw = json.loads(_gh([
        "api", f"repos/{repo}/issues/{pr}/comments?per_page=100", "--paginate", "--slurp",
    ]))
    if not isinstance(raw, list):
        raise RuntimeError("GitHub returned invalid provenance comments")
    pages = raw if raw and isinstance(raw[0], list) else [raw]
    target = delivery_evidence_manifest(
        manifest_marker,
        expected_repo=repo,
        expected_pr=int(pr),
        expected_head=head,
    )
    if target is None:
        raise RuntimeError("Builder evidence provenance manifest is invalid")
    matching: list[dict[str, object]] = []
    for page in pages:
        if not isinstance(page, list):
            raise RuntimeError("GitHub returned invalid provenance comments")
        for comment in page:
            if not isinstance(comment, dict):
                raise RuntimeError("GitHub returned invalid provenance comment")
            user = comment.get("user") or {}
            if not (
                isinstance(user, dict)
                and user.get("type") == "Bot"
                and user.get("login") == builder_app_login
                and DELIVERY_PROVENANCE in str(comment.get("body") or "")
            ):
                continue
            manifest = delivery_evidence_manifest(
                str(comment.get("body") or ""),
                expected_repo=repo,
                expected_pr=int(pr),
                expected_head=head,
            )
            if manifest is not None:
                if evidence_manifests_match(manifest, target):
                    matching.append(comment)
    body = (
        f"{DELIVERY_PROVENANCE}\n"
        "<details><summary>Builder evidence provenance</summary>\n\n"
        f"Authenticated media manifest for `{head}`.\n\n{manifest_marker}\n\n"
        "</details>"
    )
    payload = json.dumps({"body": body})
    if matching:
        canonical = max(
            matching,
            key=lambda item: item.get("id") if isinstance(item.get("id"), int) else -1,
        )
        comment_id = canonical.get("id")
        if not isinstance(comment_id, int):
            raise RuntimeError("Builder evidence provenance comment has no id")
        endpoint = f"repos/{repo}/issues/comments/{comment_id}"
        method = "PATCH"
    else:
        endpoint = f"repos/{repo}/issues/{pr}/comments"
        method = "POST"
    created = json.loads(_gh(["api", endpoint, "-X", method, "--input", "-"], stdin=payload))
    user = created.get("user") or {}
    if (
        not isinstance(user, dict)
        or user.get("type") != "Bot"
        or user.get("login") != builder_app_login
        or str(created.get("body") or "") != body
    ):
        raise RuntimeError("Builder App could not authenticate evidence provenance")


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
    *,
    builder_app_login: str = "agent-factory-builder[bot]",
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
        manifest_marker = _evidence_manifest(repo, pr, head, attachments, urls, content)
        content = "\n\n".join([
            content.rstrip(),
            manifest_marker,
        ])
        _publish_evidence_provenance(
            repo, pr, head, manifest_marker, builder_app_login
        )
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
        manifest = delivery_evidence_manifest(
            published_delivery,
            expected_repo=repo,
            expected_pr=int(pr),
            expected_head=head,
        )
        if manifest is None or tuple(manifest) != tuple(urls.values()):
            raise RuntimeError("canonical delivery omitted Builder media provenance")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--status", required=True, choices=sorted(VALID_STATUSES))
    parser.add_argument("--body-file", type=Path, required=True)
    parser.add_argument("--attach", type=Path, action="append", default=[])
    parser.add_argument("--config", type=Path, default=Path(".agent-factory/config.json"))
    args = parser.parse_args()
    builder_app_login = (
        load_config(args.config).builder.app_login
        if args.config.is_file()
        else "agent-factory-builder[bot]"
    )
    publish(
        args.repo,
        args.pr,
        args.head,
        args.status,
        args.body_file.read_text(encoding="utf-8"),
        tuple(args.attach),
        builder_app_login=builder_app_login,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
