"""GitHub adapter for Steward intake, dispatch, and blocker routing."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import load_config
from .protocol import decode_data, encode_data


def _gh(args: list[str], *, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=stdin, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def _flatten_pages(text: str) -> list[dict[str, Any]]:
    value = json.loads(text)
    if not isinstance(value, list):
        return []
    if value and all(isinstance(page, list) for page in value):
        return [item for page in value for item in page if isinstance(item, dict)]
    return [item for item in value if isinstance(item, dict)]


def _upsert_issue_comment(repo: str, issue: str, marker: str, body: str) -> None:
    comments = _flatten_pages(
        _gh(["api", f"repos/{repo}/issues/{issue}/comments", "--paginate", "--slurp"])
    )
    existing = next(
        (item for item in comments if marker in str(item.get("body") or "")), None
    )
    payload = json.dumps({"body": body})
    if existing and isinstance(existing.get("id"), int):
        _gh(
            ["api", f"repos/{repo}/issues/comments/{existing['id']}", "-X", "PATCH", "--input", "-"],
            stdin=payload,
        )
    else:
        _gh(["api", f"repos/{repo}/issues/{issue}/comments", "-X", "POST", "--input", "-"], stdin=payload)


def format_status(marker: str, issue: str, state: str, next_owner: str, detail: str) -> str:
    machine = {
        "version": 1,
        "role": "steward",
        "issue": int(issue),
        "state": state,
        "next_owner": next_owner.lower(),
    }
    return "\n".join(
        [
            marker,
            "",
            "## Steward",
            f"**{state.replace('_', ' ').title()} → {next_owner}**",
            "",
            detail,
            "",
            encode_data(machine),
            "",
        ]
    )


def run(repo: str, issue: str, config_path: Path) -> str:
    if not os.environ.get("GH_TOKEN"):
        raise RuntimeError("GH_TOKEN must be a Steward App installation token")
    config = load_config(config_path)
    for name, color, description in (
        ("agent:steward", "8250DF", "Builder needs Steward routing"),
        (config.steward.retry_label, "FBCA04", "Steward should retry this issue"),
    ):
        _gh(
            [
                "label", "create", name, "--repo", repo, "--color", color,
                "--description", description, "--force",
            ]
        )
    item = json.loads(
        _gh(["issue", "view", issue, "--repo", repo, "--json", "state,title,body,labels"])
    )
    labels = {
        str(label.get("name") or "")
        for label in item.get("labels") or []
        if isinstance(label, dict)
    }
    if str(item.get("state") or "").upper() != "OPEN":
        state, next_owner = "closed", "Nobody"
        detail = "The issue is closed, so no implementation was dispatched."
    elif not labels.intersection(config.steward.ready_labels):
        state, next_owner = "needs_context", "Steward"
        detail = "The issue is not in a configured ready state. Steward will not dispatch speculative work."
    else:
        comments = _flatten_pages(
            _gh(["api", f"repos/{repo}/issues/{issue}/comments", "--paginate", "--slurp"])
        )
        latest_builder = None
        for comment in comments:
            data = decode_data(str(comment.get("body") or ""))
            if data and data.get("role") == "builder":
                latest_builder = data
        if latest_builder and latest_builder.get("state") == "blocked" and config.steward.retry_label not in labels:
            state, next_owner = "blocked", "Steward"
            detail = (
                "Builder returned a blocked result. Steward is holding dispatch until the "
                "issue context, task split, or repository capability is improved."
            )
        else:
            state, next_owner = "dispatched", "Builder"
            detail = (
                "The issue is open and ready. Builder is assigned from the configured base "
                "branch; implementation and verification remain repository-owned."
            )
            # A failed Builder may leave its dispatch label attached. GitHub
            # emits no labeled event when an already-present label is added,
            # so a retry must remove that stale edge before re-adding it.
            if (
                config.steward.retry_label in labels
                and config.steward.dispatch_label in labels
            ):
                _gh(
                    [
                        "issue", "edit", issue, "--repo", repo,
                        "--remove-label", config.steward.dispatch_label,
                    ]
                )
            _gh(
                [
                    "label", "create", config.steward.dispatch_label, "--repo", repo,
                    "--color", "1D76DB", "--description", "Steward assigned this issue to Builder",
                    "--force",
                ]
            )
            _gh(["issue", "edit", issue, "--repo", repo, "--add-label", config.steward.dispatch_label])
            for stale_label in ("agent:steward", config.steward.retry_label):
                if stale_label in labels:
                    _gh(["issue", "edit", issue, "--repo", repo, "--remove-label", stale_label])

    body = format_status(config.steward.marker, issue, state, next_owner, detail)
    _upsert_issue_comment(repo, issue, config.steward.marker, body)
    print(state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    run(args.repo, args.issue, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
