"""Publish runner-produced evidence into Builder's canonical pull-request body."""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote


DELIVERY_START = "<!-- agent-factory:builder-delivery:start -->"
DELIVERY_END = "<!-- agent-factory:builder-delivery:end -->"
DELIVERY_STATUS = "<!-- agent-factory:builder-delivery-status:{status} -->"
DELIVERY_HEAD = "<!-- agent-factory:builder-delivery-head:{head} -->"
VALID_STATUSES = {"pending", "ready", "failed"}
EVIDENCE_BRANCH = "agent-factory-evidence"


def _gh(args: list[str], *, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=stdin, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def _api(repo: str, endpoint: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    path = f"repos/{repo}"
    if endpoint:
        path += f"/{endpoint.lstrip('/')}"
    args = ["api", path]
    if method != "GET":
        args.extend(["-X", method])
    stdin = None
    if payload is not None:
        args.extend(["--input", "-"])
        stdin = json.dumps(payload)
    output = _gh(args, stdin=stdin)
    return json.loads(output) if output.strip() else {}


def _evidence_ref(repo: str) -> dict:
    try:
        return _api(repo, f"git/ref/heads/{EVIDENCE_BRANCH}")
    except RuntimeError as error:
        if "404" not in str(error):
            raise
    repository = _api(repo, "")
    default_branch = str(repository["default_branch"])
    base = _api(repo, f"git/ref/heads/{default_branch}")
    try:
        return _api(
            repo,
            "git/refs",
            method="POST",
            payload={
                "ref": f"refs/heads/{EVIDENCE_BRANCH}",
                "sha": base["object"]["sha"],
            },
        )
    except RuntimeError as error:
        # Another delivery may have created the shared ref concurrently.
        if "422" not in str(error):
            raise
        return _api(repo, f"git/ref/heads/{EVIDENCE_BRANCH}")


def _publish_attachments(
    repo: str,
    pr: str,
    head: str,
    attachments: tuple[Path, ...],
) -> dict[str, str]:
    blobs: list[tuple[Path, str]] = []
    for path in attachments:
        blob = _api(
            repo,
            "git/blobs",
            method="POST",
            payload={
                "content": base64.b64encode(path.read_bytes()).decode("ascii"),
                "encoding": "base64",
            },
        )
        blobs.append((path, str(blob["sha"])))

    remote_paths = {
        path: f"pr-{pr}/{head}/{index:02d}-{path.name}"
        for index, (path, _) in enumerate(blobs, start=1)
    }
    ref = _evidence_ref(repo)
    for attempt in range(3):
        parent = str(ref["object"]["sha"])
        commit = _api(repo, f"git/commits/{parent}")
        tree = _api(
            repo,
            "git/trees",
            method="POST",
            payload={
                "base_tree": commit["tree"]["sha"],
                "tree": [
                    {
                        "path": remote_paths[path],
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_sha,
                    }
                    for path, blob_sha in blobs
                ],
            },
        )
        evidence_commit = _api(
            repo,
            "git/commits",
            method="POST",
            payload={
                "message": f"evidence: PR #{pr} at {head[:12]}",
                "tree": tree["sha"],
                "parents": [parent],
            },
        )
        try:
            _api(
                repo,
                f"git/refs/heads/{EVIDENCE_BRANCH}",
                method="PATCH",
                payload={"sha": evidence_commit["sha"], "force": False},
            )
            break
        except RuntimeError as error:
            if attempt == 2 or "422" not in str(error):
                raise
            ref = _api(repo, f"git/ref/heads/{EVIDENCE_BRANCH}")
    evidence_sha = quote(str(evidence_commit["sha"]), safe="")
    return {
        str(path): (
            f"https://github.com/{repo}/raw/{evidence_sha}/"
            f"{quote(remote_paths[path], safe='/')}"
        )
        for path, _ in blobs
    }


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
        missing = [str(path) for path in attachments if not path.is_file()]
        if missing:
            raise ValueError(f"Builder delivery attachments do not exist: {', '.join(missing)}")
        urls = _publish_attachments(repo, pr, head, attachments)
        for local_path, url in urls.items():
            if Path(local_path).suffix.lower() in {".mp4", ".mov", ".webm"}:
                content = content.replace(
                    f"![]({local_path})", f"[Open interaction recording]({url})"
                )
            content = content.replace(local_path, url)
        # Uploading may take long enough for a new Builder revision to arrive.
        # Re-read both the head and body so stale media can never overwrite it.
        meta = json.loads(_gh([
            "pr", "view", pr, "--repo", repo, "--json", "headRefOid,body",
        ]))
        current_head = str(meta.get("headRefOid") or "")
        if current_head != head:
            raise RuntimeError(
                f"refusing stale Builder evidence for {head[:7]}; current head is {current_head[:7]}"
            )
    updated = replace_delivery(
        str(meta.get("body") or ""), format_delivery(status, content, head=head)
    )
    _gh(
        ["api", f"repos/{repo}/pulls/{pr}", "-X", "PATCH", "--input", "-"],
        stdin=json.dumps({"body": updated}),
    )
    published = json.loads(_gh([
        "pr", "view", pr, "--repo", repo, "--json", "headRefOid,body",
    ]))
    published_head = str(published.get("headRefOid") or "")
    if published_head != head or delivery_head(str(published.get("body") or "")) != head:
        raise RuntimeError(
            f"Builder evidence publication raced a new head; expected {head[:7]}, "
            f"found {published_head[:7]}"
        )


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
