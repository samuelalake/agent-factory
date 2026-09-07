"""Publish runner-produced evidence into Builder's canonical pull-request body."""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path


DELIVERY_START = "<!-- agent-factory:builder-delivery:start -->"
DELIVERY_END = "<!-- agent-factory:builder-delivery:end -->"
DELIVERY_STATUS = "<!-- agent-factory:builder-delivery-status:{status} -->"
VALID_STATUSES = {"pending", "ready", "failed"}


def _gh(args: list[str], *, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=stdin, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def format_delivery(status: str, content: str) -> str:
    if status not in VALID_STATUSES:
        raise ValueError(f"unsupported Builder delivery status: {status}")
    clean = content.strip()
    return "\n".join([
        DELIVERY_START,
        DELIVERY_STATUS.format(status=status),
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


def delivery_status(body: str) -> str | None:
    for status in VALID_STATUSES:
        if DELIVERY_STATUS.format(status=status) in body:
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
        status = delivery_status(body)
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
    updated = replace_delivery(
        str(meta.get("body") or ""), format_delivery(status, content)
    )
    if attachments:
        missing = [str(path) for path in attachments if not path.is_file()]
        if missing:
            raise ValueError(f"Builder delivery attachments do not exist: {', '.join(missing)}")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md") as body_file:
            body_file.write(updated)
            body_file.flush()
            args = ["pr", "edit", pr, "--repo", repo, "--body-file", body_file.name]
            for path in attachments:
                args.extend(["--attach", str(path)])
            _gh(args)
    else:
        _gh(
            ["api", f"repos/{repo}/pulls/{pr}", "-X", "PATCH", "--input", "-"],
            stdin=json.dumps({"body": updated}),
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
