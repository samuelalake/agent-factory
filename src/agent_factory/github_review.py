"""Generic current-head reviewer adapter for reusable workflows."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .app_auth import get_installation_token
from .config import load_config
from .context import discover_context
from .model import complete
from .protocol import encode_data, extract_json_reply


def _gh(args: list[str], *, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=stdin, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def normalize_review(raw: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    for item in raw.get("findings") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "").upper()
        if severity not in {"P1", "P2", "P3"}:
            continue
        path = str(item.get("file") or "").strip()
        line = item.get("line")
        location = path + (f":{line}" if path and isinstance(line, int) else "")
        findings.append({
            "severity": severity,
            "key": location or "review-wide",
            "title": str(item.get("title") or "untitled finding").strip(),
            "reasoning": str(item.get("reasoning") or "").strip(),
            "suggestion": str(item.get("suggestion") or "").strip(),
        })
    approve = bool(raw.get("approve")) and not any(f["severity"] == "P1" for f in findings)
    return {"summary": str(raw.get("summary") or "").strip(), "approve": approve, "findings": findings}


def format_body(marker: str, head_sha: str, review: dict[str, Any]) -> str:
    counts = {severity: 0 for severity in ("P1", "P2", "P3")}
    for finding in review["findings"]:
        counts[finding["severity"]] += 1
    lines = [
        marker,
        "",
        "## Agent Factory review",
        f"HEAD: `{head_sha}` · verdict: **{'approve' if review['approve'] else 'request changes'}**",
        "",
        review["summary"] or "No summary provided.",
        "",
        *(f"### {severity} ({counts[severity]})" for severity in ("P1", "P2", "P3")),
        "",
        "### Findings",
        "",
    ]
    if not review["findings"]:
        lines.append("_No findings._")
    for finding in review["findings"]:
        lines.extend([
            f"- **[{finding['severity']}] `{finding['key']}`** {finding['title']}",
            *( [f"  - {finding['reasoning']}"] if finding["reasoning"] else [] ),
            *( [f"  - _suggestion:_ {finding['suggestion']}"] if finding["suggestion"] else [] ),
        ])
    machine = {
        "version": 1,
        "head_sha": head_sha,
        "verdict": "approve" if review["approve"] else "request_changes",
        "findings": [{"severity": f["severity"], "key": f["key"]} for f in review["findings"]],
    }
    lines.extend(["", encode_data(machine)])
    return "\n".join(lines) + "\n"


def run(
    repo: str,
    pr: str,
    root: Path,
    config_path: Path,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> None:
    # A distinct App identity is required for a formal approval that can gate a
    # consumer pull request. Keep its short-lived token out of files and logs.
    os.environ["GH_TOKEN"] = get_installation_token(repo)
    config = load_config(config_path)
    meta = json.loads(_gh(["pr", "view", pr, "--repo", repo, "--json", "headRefOid,title,body"]))
    diff = _gh(["pr", "diff", pr, "--repo", repo])
    encoded = diff.encode()
    omitted = max(0, len(encoded) - config.review.max_diff_bytes)
    diff = encoded[: config.review.max_diff_bytes].decode(errors="ignore")
    task = f"{meta.get('title', '')}\n{meta.get('body', '')}\n{diff}"
    discovered = discover_context(root, config.project, task, role="review")
    context = [
        f"## {document.kind}: {document.path}\n\n{document.content}"
        for document in discovered
    ]
    system = (
        f"You are the required code reviewer for {config.project.name}. "
        "Return only JSON with summary:string, approve:boolean, and findings:array. "
        "Each finding has severity P1|P2|P3, file, optional integer line, title, "
        "reasoning, and suggestion. P1 is merge-blocking. Do not approve a partial diff."
    )
    user = "\n\n".join(context + [
        f"## Pull request\n\n{meta.get('title','')}\n\n{meta.get('body','')}",
        f"## Diff\n\n```diff\n{diff}\n```",
    ])
    provider = provider_override or config.review.provider
    model = model_override or config.review.model
    reply = complete(provider, model, system, user, os.environ.get("MODEL_API_KEY", ""))
    raw = normalize_review(extract_json_reply(reply))
    if omitted:
        raw["approve"] = False
        raw["findings"].insert(0, {
            "severity": "P1", "key": "agent-factory://truncated-diff",
            "title": f"Reviewer input omitted {omitted} bytes", "reasoning": "The full diff was not reviewed.",
            "suggestion": "Split the pull request or raise the configured review limit.",
        })
    body = format_body(config.review.marker, meta["headRefOid"], raw)
    event = "APPROVE" if raw["approve"] else "REQUEST_CHANGES"
    payload = json.dumps({"body": body, "event": event, "commit_id": meta["headRefOid"]})
    _gh(["api", f"repos/{repo}/pulls/{pr}/reviews", "-X", "POST", "--input", "-"], stdin=payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    args = parser.parse_args()
    run(args.repo, args.pr, args.root, args.config, args.provider, args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
