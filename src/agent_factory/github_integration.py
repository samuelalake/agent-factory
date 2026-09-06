"""Deterministic integration wait and Steward-authored promotion decision."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from .config import load_config
from .protocol import encode_data


SUCCESS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
FAILURE = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE"}


def _gh(args: list[str], *, token: str, stdin: str | None = None) -> str:
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    result = subprocess.run(["gh", *args], input=stdin, text=True, capture_output=True, env=env)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def check_state(required: tuple[str, ...], rollup: list[dict[str, Any]]) -> tuple[str, str]:
    by_name: dict[str, str] = {}
    for item in rollup:
        name = str(item.get("name") or item.get("context") or "")
        state = str(item.get("conclusion") or item.get("state") or item.get("status") or "PENDING").upper()
        if name:
            by_name[name] = state
    for name in required:
        state = by_name.get(name)
        if state is None:
            return "pending", f"waiting for required check {name}"
        if state in FAILURE:
            return "failure", f"required check {name} concluded {state.lower()}"
        if state not in SUCCESS:
            return "pending", f"required check {name} is {state.lower()}"
    return "success", "all integration requirements passed"


def _set_status(repo: str, sha: str, context: str, state: str, description: str, token: str) -> None:
    payload = json.dumps({"state": state, "context": context, "description": description[:140]})
    _gh(["api", f"repos/{repo}/statuses/{sha}", "-X", "POST", "--input", "-"], token=token, stdin=payload)


def _upsert_steward_comment(repo: str, pr: str, marker: str, body: str, token: str) -> None:
    comments = json.loads(
        _gh(["api", f"repos/{repo}/issues/{pr}/comments", "--paginate"], token=token)
    )
    existing = next(
        (item for item in comments if marker in str(item.get("body") or "")), None
    )
    payload = json.dumps({"body": body})
    if existing and isinstance(existing.get("id"), int):
        endpoint = f"repos/{repo}/issues/comments/{existing['id']}"
        _gh(["api", endpoint, "-X", "PATCH", "--input", "-"], token=token, stdin=payload)
    else:
        endpoint = f"repos/{repo}/issues/{pr}/comments"
        _gh(["api", endpoint, "-X", "POST", "--input", "-"], token=token, stdin=payload)


def format_integration(
    marker: str,
    head: str,
    state: str,
    detail: str,
    environment: str,
) -> str:
    next_owner = "Landing" if state == "ready" else "Builder" if state == "failed" else "Integration"
    data = {
        "version": 1,
        "role": "steward",
        "state": state,
        "head_sha": head,
        "environment": environment,
        "next_owner": next_owner.lower(),
    }
    return "\n".join(
        [
            marker,
            "",
            "## Steward · integration",
            f"**{state.title()} → {next_owner}**",
            "",
            detail,
            "",
            f"Environment: `{environment}` · HEAD: `{head}`",
            "",
            encode_data(data),
            "",
        ]
    )


def run(
    repo: str,
    pr: str,
    config_path: Path,
    *,
    timeout_seconds: int = 1800,
    poll_seconds: int = 15,
) -> str:
    github_token = os.environ.get("GITHUB_TOKEN", "")
    steward_token = os.environ.get("STEWARD_TOKEN", "")
    if not github_token or not steward_token:
        raise RuntimeError("GITHUB_TOKEN and STEWARD_TOKEN are required")
    config = load_config(config_path)
    required = tuple(dict.fromkeys((*config.gate.required_checks, config.gate.context)))
    deadline = time.monotonic() + timeout_seconds
    last_detail = "waiting for integration requirements"
    meta: dict[str, Any] = {}
    while True:
        meta = json.loads(
            _gh(
                ["pr", "view", pr, "--repo", repo, "--json", "headRefOid,mergeable,statusCheckRollup,url"],
                token=github_token,
            )
        )
        state, last_detail = check_state(required, meta.get("statusCheckRollup") or [])
        if str(meta.get("mergeable") or "UNKNOWN") == "CONFLICTING":
            state, last_detail = "failure", "pull request conflicts with the integration base"
        if state != "pending" or time.monotonic() >= deadline:
            break
        time.sleep(poll_seconds)

    head = str(meta.get("headRefOid") or "")
    if state == "success":
        _set_status(
            repo, head, config.integration.status_context, "success",
            "integration policy passed; deterministic landing authorized", github_token,
        )
        body = format_integration(
            config.integration.marker,
            head,
            "ready",
            "The current head passed review, repository verification, and the configured integration policy.",
            config.integration.environment,
        )
        _upsert_steward_comment(repo, pr, config.integration.marker, body, steward_token)
        if config.integration.automatic_promotion:
            _gh(["pr", "merge", pr, "--repo", repo, "--auto", "--squash"], token=github_token)
        print("ready")
        return "ready"

    final_state = "error" if state == "failure" else "pending"
    _set_status(repo, head, config.integration.status_context, final_state, last_detail, github_token)
    body = format_integration(
        config.integration.marker,
        head,
        "failed" if state == "failure" else "waiting",
        last_detail,
        config.integration.environment,
    )
    _upsert_steward_comment(repo, pr, config.integration.marker, body, steward_token)
    if state == "failure":
        raise RuntimeError(last_detail)
    raise TimeoutError(last_detail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    run(args.repo, args.pr, args.config, timeout_seconds=args.timeout_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
