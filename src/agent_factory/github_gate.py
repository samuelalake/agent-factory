"""GitHub collection/publication adapter around the pure gate engine."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from .config import load_config
from .gate import Check, Finding, GateInput, Review, evaluate_gate
from .protocol import decode_data


def _gh(args: list[str], *, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=stdin, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def _flatten_pages(text: str) -> list[dict]:
    value = json.loads(text)
    if not isinstance(value, list):
        return []
    if value and all(isinstance(page, list) for page in value):
        return [item for page in value for item in page if isinstance(item, dict)]
    return [item for item in value if isinstance(item, dict)]


def _issue_numbers(body: str) -> set[int]:
    return {int(value) for value in re.findall(r"(?<![\w])#([1-9][0-9]*)", body)}


def _issue_is_valid_followup(issue: dict) -> bool:
    """Accept open issues and completed issues, never not-planned closures."""
    state = str(issue.get("state") or "").upper()
    reason = str(issue.get("stateReason") or "").lower()
    return state == "OPEN" or (state == "CLOSED" and reason != "not_planned")


def run(repo: str, pr: str, config_path: Path) -> str:
    config = load_config(config_path)
    meta = json.loads(_gh([
        "pr", "view", pr, "--repo", repo, "--json",
        "headRefOid,mergeable,statusCheckRollup,body",
    ]))
    head = meta["headRefOid"]
    checks = tuple(
        Check(str(item.get("name") or item.get("context") or ""), str(item.get("conclusion") or item.get("state") or "pending"))
        for item in meta.get("statusCheckRollup") or []
    )
    reviews = _flatten_pages(_gh(["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate", "--slurp"]))
    selected = None
    data = None
    for candidate in reviews:
        body = str(candidate.get("body") or "")
        decoded = decode_data(body)
        if config.review.marker in body and decoded is not None and candidate.get("state") != "DISMISSED":
            selected, data = candidate, decoded

    review = None
    if selected is not None and data is not None:
        refs = _issue_numbers(str(meta.get("body") or "")) if config.gate.allow_followup_issues else set()
        issue_bodies: list[str] = []
        for number in refs:
            try:
                issue = json.loads(_gh(["issue", "view", str(number), "--repo", repo, "--json", "body,state,stateReason"]))
            except RuntimeError:
                continue
            if _issue_is_valid_followup(issue):
                issue_bodies.append(str(issue.get("body") or ""))
        findings = []
        for raw in data.get("findings") or []:
            key = str(raw.get("key") or "review-wide")
            tracked = any(f"#{pr}" in body or f"/pull/{pr}" in body or key in body for body in issue_bodies)
            findings.append(Finding(str(raw.get("severity") or ""), key, tracked))
        state = str(selected.get("state") or "COMMENTED")
        verdict = "approve" if state == "APPROVED" else "request_changes" if state == "CHANGES_REQUESTED" else "comment"
        review = Review(str(data.get("head_sha") or selected.get("commit_id") or ""), verdict, tuple(findings))

    mergeable_raw = str(meta.get("mergeable") or "UNKNOWN")
    mergeable = True if mergeable_raw == "MERGEABLE" else False if mergeable_raw == "CONFLICTING" else None
    decision = evaluate_gate(GateInput(mergeable, head, checks, config.gate.required_checks, review))
    payload = json.dumps({
        "state": decision.state,
        "context": config.gate.context,
        "description": decision.description[:140],
    })
    _gh(["api", f"repos/{repo}/statuses/{head}", "-X", "POST", "--input", "-"], stdin=payload)
    print(f"{decision.state}: {decision.code}: {decision.description}")
    return decision.state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    run(args.repo, args.pr, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
