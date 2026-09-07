"""Deterministic integration wait and Steward-authored promotion decision."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from .config import load_config
from .github_gate import evaluate_and_publish as recompute_gate
from .protocol import decode_data, encode_data


SUCCESS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
FAILURE = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE"}
FOLLOWUP_LINK_MARKER = "<!-- agent-factory:review-followup-link -->"


def linked_issue_numbers(body: str) -> tuple[str, ...]:
    matches = re.findall(
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b",
        body,
        flags=re.IGNORECASE,
    )
    return tuple(dict.fromkeys(matches))


def route_failure(repo: str, meta: dict[str, Any], config: Any, token: str) -> str:
    issues = linked_issue_numbers(str(meta.get("body") or ""))
    if not issues:
        return "No linked issue was available for automatic Builder routing."
    reviewer_unavailable = any(
        config.review.failure_marker in str(review.get("body") or "")
        and (
            (data := decode_data(str(review.get("body") or ""))) is not None
            and data.get("head_sha") == str(meta.get("headRefOid") or "")
        )
        for review in meta.get("reviews") or []
    )
    attempts = sum(
        1
        for commit in meta.get("commits") or []
        if str(commit.get("messageHeadline") or "").startswith("feat: implement issue #")
    )
    if reviewer_unavailable:
        label = "agent:steward"
        route = (
            "Reviewer providers did not produce a valid current-head verdict; "
            "Steward retained the blocker instead of assigning an unreviewable revision to Builder."
        )
    elif attempts >= config.builder.max_revision_attempts:
        label = "agent:steward"
        route = (
            f"Automatic Builder revisions reached the configured limit of "
            f"{config.builder.max_revision_attempts}; Steward retained the blocker for intervention."
        )
    else:
        label = config.steward.retry_label
        route = (
            f"Steward routed the current-head failure back to Builder "
            f"(revision {attempts + 1} of {config.builder.max_revision_attempts})."
        )
    for issue in issues:
        _gh(
            ["issue", "edit", issue, "--repo", repo, "--add-label", label],
            token=token,
        )
    return route


def _flatten_pages(text: str) -> list[dict[str, Any]]:
    value = json.loads(text)
    if not isinstance(value, list):
        return []
    if value and all(isinstance(page, list) for page in value):
        return [item for page in value for item in page if isinstance(item, dict)]
    return [item for item in value if isinstance(item, dict)]


def current_followup_findings(
    repo: str,
    pr: str,
    head: str,
    review_marker: str,
    token: str,
) -> list[dict[str, str]]:
    reviews = _flatten_pages(
        _gh(
            ["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate", "--slurp"],
            token=token,
        )
    )
    selected: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    for candidate in reviews:
        body = str(candidate.get("body") or "")
        decoded = decode_data(body)
        if (
            review_marker in body
            and isinstance(decoded, dict)
            and str(decoded.get("head_sha") or candidate.get("commit_id") or "") == head
            and str(candidate.get("state") or "").upper() == "APPROVED"
        ):
            selected, data = candidate, decoded
    if selected is None or data is None:
        return []
    findings: list[dict[str, str]] = []
    for raw in data.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        severity = str(raw.get("severity") or "").upper()
        if severity not in {"P2", "P3"}:
            continue
        findings.append({
            "severity": severity,
            "key": str(raw.get("key") or "review-wide"),
            "title": str(raw.get("title") or "Reviewer follow-up").strip(),
            "reasoning": str(raw.get("reasoning") or "").strip(),
            "suggestion": str(raw.get("suggestion") or "").strip(),
        })
    return findings


def _followup_marker(pr: str) -> str:
    return f"<!-- agent-factory:review-followup pr={pr} -->"


def _format_followup_issue(
    repo: str,
    pr: str,
    head: str,
    findings: list[dict[str, str]],
) -> str:
    lines = [
        _followup_marker(pr),
        "",
        f"# Reviewer follow-up for PR #{pr}",
        "",
        f"Source: https://github.com/{repo}/pull/{pr}",
        f"Reviewed head: `{head}`",
        "",
        "Steward consolidated the non-blocking Reviewer findings below so the delivery can remain traceable without expanding the current pull request.",
        "",
        "## Acceptance criteria",
        "",
    ]
    for finding in findings:
        lines.append(f"- [ ] **[{finding['severity']}] `{finding['key']}` — {finding['title']}")
        if finding["reasoning"]:
            lines.append(f"  - Why: {finding['reasoning']}")
        if finding["suggestion"]:
            lines.append(f"  - Suggested direction: {finding['suggestion']}")
    lines.extend([
        "",
        "## Steward constraints",
        "",
        "- Re-evaluate scope and dependencies before marking this issue ready.",
        "- Do not treat this issue as an approval of unrelated cleanup.",
        "",
    ])
    return "\n".join(lines)


def ensure_followup_issue(
    repo: str,
    pr: str,
    head: str,
    pr_body: str,
    review_marker: str,
    github_token: str,
    steward_token: str,
) -> int:
    findings = current_followup_findings(repo, pr, head, review_marker, github_token)
    if not findings:
        raise RuntimeError("orphan-findings gate had no current approved P2/P3 findings")
    marker = _followup_marker(pr)
    issues = json.loads(
        _gh(
            ["issue", "list", "--repo", repo, "--state", "open", "--limit", "100", "--json", "number,body"],
            token=steward_token,
        )
    )
    existing = next(
        (item for item in issues if marker in str(item.get("body") or "")), None
    )
    issue_body = _format_followup_issue(repo, pr, head, findings)
    if existing and isinstance(existing.get("number"), int):
        number = existing["number"]
        payload = json.dumps({"title": f"Reviewer follow-up for PR #{pr}", "body": issue_body})
        _gh(
            ["api", f"repos/{repo}/issues/{number}", "-X", "PATCH", "--input", "-"],
            token=steward_token,
            stdin=payload,
        )
    else:
        payload = json.dumps({"title": f"Reviewer follow-up for PR #{pr}", "body": issue_body})
        created = json.loads(
            _gh(
                ["api", f"repos/{repo}/issues", "-X", "POST", "--input", "-"],
                token=steward_token,
                stdin=payload,
            )
        )
        number = int(created["number"])
    link_block = f"{FOLLOWUP_LINK_MARKER}\nReviewer follow-up: #{number}"
    if FOLLOWUP_LINK_MARKER in pr_body:
        updated_body = re.sub(
            rf"{re.escape(FOLLOWUP_LINK_MARKER)}\nReviewer follow-up: #\d+",
            link_block,
            pr_body,
        )
    else:
        updated_body = pr_body.rstrip() + "\n\n" + link_block + "\n"
    if updated_body != pr_body:
        _gh(
            ["api", f"repos/{repo}/pulls/{pr}", "-X", "PATCH", "--input", "-"],
            token=steward_token,
            stdin=json.dumps({"body": updated_body}),
        )
    return number


def review_followup_issue_number(pr_body: str) -> int | None:
    match = re.search(
        rf"{re.escape(FOLLOWUP_LINK_MARKER)}\nReviewer follow-up: #(\d+)",
        pr_body,
    )
    return int(match.group(1)) if match else None


def queue_followup_for_steward(repo: str, issue: int, token: str) -> None:
    _gh(
        ["issue", "edit", str(issue), "--repo", repo, "--add-label", "agent:steward"],
        token=token,
    )


def report_landing_permission_failure(repo: str, pr: str, config_path: Path) -> None:
    github_token = os.environ.get("GITHUB_TOKEN", "")
    steward_token = os.environ.get("STEWARD_TOKEN", "")
    if not github_token or not steward_token:
        raise RuntimeError("GITHUB_TOKEN and STEWARD_TOKEN are required")
    config = load_config(config_path)
    meta = json.loads(
        _gh(["pr", "view", pr, "--repo", repo, "--json", "headRefOid"], token=github_token)
    )
    head = str(meta.get("headRefOid") or "")
    detail = (
        "Steward cannot land this change because its GitHub App installation does not "
        "grant Contents: write. Update and approve that permission; Issues: write and "
        "Pull requests: write remain required for Steward's project record."
    )
    _set_status(
        repo, head, config.integration.status_context, "error", detail, github_token
    )
    body = format_integration(
        config.integration.marker,
        head,
        "failed",
        detail,
        config.integration.environment,
        next_owner="Steward",
    )
    _upsert_steward_comment(repo, pr, config.integration.marker, body, steward_token)


def close_delivered_issues(repo: str, pr_body: str, config: Any, token: str) -> None:
    agent_labels = {config.steward.dispatch_label, config.steward.retry_label, "agent:steward"}
    for issue in linked_issue_numbers(pr_body):
        raw = json.loads(
            _gh(["issue", "view", issue, "--repo", repo, "--json", "labels"], token=token)
        )
        labels = [
            str(item.get("name") or "")
            for item in raw.get("labels") or []
            if str(item.get("name") or "") not in agent_labels
        ]
        payload = json.dumps({"state": "closed", "state_reason": "completed", "labels": labels})
        _gh(
            ["api", f"repos/{repo}/issues/{issue}", "-X", "PATCH", "--input", "-"],
            token=token,
            stdin=payload,
        )


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
    next_owner: str | None = None,
) -> str:
    next_owner = next_owner or (
        "Landing" if state == "ready" else "Builder" if state == "failed" else "Integration"
    )
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
        # Required repository checks can finish after the Reviewer verdict. Do
        # not depend on another webhook to refresh the protected Gate:
        # integration owns keeping that current-head decision fresh.
        os.environ["GH_TOKEN"] = github_token
        decision = recompute_gate(repo, pr, config_path)
        meta = json.loads(
            _gh(
                [
                    "pr", "view", pr, "--repo", repo, "--json",
                    "headRefOid,mergeable,statusCheckRollup,url,body,commits,reviews",
                ],
                token=github_token,
            )
        )
        if decision.code == "orphan-findings":
            ensure_followup_issue(
                repo,
                pr,
                str(meta.get("headRefOid") or ""),
                str(meta.get("body") or ""),
                config.review.marker,
                github_token,
                steward_token,
            )
            recompute_gate(repo, pr, config_path)
            meta = json.loads(
                _gh(
                    [
                        "pr", "view", pr, "--repo", repo, "--json",
                        "headRefOid,mergeable,statusCheckRollup,url,body,commits,reviews",
                    ],
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
        pr_body = str(meta.get("body") or "")
        followup_issue = review_followup_issue_number(pr_body)
        if config.integration.automatic_promotion:
            try:
                _gh(
                    ["pr", "merge", pr, "--repo", repo, "--squash", "--delete-branch"],
                    token=steward_token,
                )
            except RuntimeError as exc:
                detail = (
                    "Landing passed policy but GitHub rejected Steward's merge. "
                    "Confirm that the Steward App has Contents: write and Pull requests: write. "
                    f"GitHub response: {str(exc)[:500]}"
                )
                _set_status(
                    repo, head, config.integration.status_context, "error", detail, github_token,
                )
                body = format_integration(
                    config.integration.marker,
                    head,
                    "failed",
                    detail,
                    config.integration.environment,
                    next_owner="Steward",
                )
                _upsert_steward_comment(
                    repo, pr, config.integration.marker, body, steward_token
                )
                raise RuntimeError(detail) from exc
            close_delivered_issues(repo, pr_body, config, steward_token)
            if followup_issue is not None:
                queue_followup_for_steward(repo, followup_issue, steward_token)
            detail = (
                "Steward merged the current head after review, repository verification, "
                "and the configured integration policy passed."
            )
            comment_state = "landed"
        else:
            detail = (
                "The current head passed review, repository verification, and the "
                "configured integration policy."
            )
            comment_state = "ready"
        _set_status(
            repo, head, config.integration.status_context, "success",
            "integration policy passed; deterministic landing completed"
            if config.integration.automatic_promotion
            else "integration policy passed; deterministic landing authorized",
            github_token,
        )
        body = format_integration(
            config.integration.marker,
            head,
            comment_state,
            detail,
            config.integration.environment,
            next_owner=config.integration.environment.title()
            if comment_state == "landed"
            else None,
        )
        _upsert_steward_comment(repo, pr, config.integration.marker, body, steward_token)
        print("ready")
        return "ready"

    final_state = "error" if state == "failure" else "pending"
    _set_status(repo, head, config.integration.status_context, final_state, last_detail, github_token)
    route_detail = ""
    if state == "failure":
        route_detail = route_failure(repo, meta, config, steward_token)
    body = format_integration(
        config.integration.marker,
        head,
        "failed" if state == "failure" else "waiting",
        f"{last_detail}. {route_detail}" if route_detail else last_detail,
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
    parser.add_argument("--report-permission-failure", action="store_true")
    args = parser.parse_args()
    if args.report_permission_failure:
        report_landing_permission_failure(args.repo, args.pr, args.config)
        return 1
    run(args.repo, args.pr, args.config, timeout_seconds=args.timeout_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
