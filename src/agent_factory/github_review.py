"""Generic current-head reviewer adapter for reusable workflows."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .app_auth import get_installation_token
from .config import load_config
from .context import discover_context
from .model import ModelError, complete
from .protocol import encode_data, extract_json_reply


def _gh(args: list[str], *, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=stdin, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def normalize_review(raw: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for item in raw.get("findings") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "").upper()
        if severity not in {"P1", "P2", "P3"}:
            continue
        path = str(item.get("file") or "").strip()
        line = item.get("line")
        line = line if type(line) is int and line > 0 else None
        location = path + (f":{line}" if path and line is not None else "")
        findings.append({
            "severity": severity,
            "key": location or "review-wide",
            "path": path,
            "line": line,
            "title": str(item.get("title") or "untitled finding").strip(),
            "reasoning": str(item.get("reasoning") or "").strip(),
            "suggestion": str(item.get("suggestion") or "").strip(),
        })
    approve = bool(raw.get("approve")) and not any(f["severity"] == "P1" for f in findings)
    return {"summary": str(raw.get("summary") or "").strip(), "approve": approve, "findings": findings}


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def diff_right_lines(diff: str) -> dict[str, set[int]]:
    """Return RIGHT-side line numbers GitHub accepts for each file in a diff."""
    lines: dict[str, set[int]] = {}
    path: str | None = None
    right_line: int | None = None
    for raw_line in diff.splitlines():
        if raw_line.startswith("diff --git "):
            path = None
            right_line = None
            continue
        if raw_line.startswith("+++ "):
            candidate = raw_line[4:].strip()
            path = None if candidate == "/dev/null" else candidate.removeprefix("b/")
            if path is not None:
                lines.setdefault(path, set())
            continue
        match = _HUNK.match(raw_line)
        if match:
            right_line = int(match.group(1))
            continue
        if path is None or right_line is None or not raw_line:
            continue
        prefix = raw_line[0]
        if prefix == "\\":
            continue
        if prefix == "-":
            continue
        if prefix in {"+", " "}:
            lines[path].add(right_line)
            right_line += 1
    return lines


def format_inline_comment(finding: dict[str, Any]) -> str:
    lines = [f"**[{finding['severity']}] {finding['title']}**"]
    if finding["reasoning"]:
        lines.extend(["", finding["reasoning"]])
    if finding["suggestion"]:
        lines.extend(["", f"Suggested change: {finding['suggestion']}"])
    return "\n".join(lines)


def partition_findings(
    findings: list[dict[str, Any]], diff: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split findings into GitHub-inline comments and summary-only findings."""
    valid_lines = diff_right_lines(diff)
    inline: list[dict[str, Any]] = []
    summary_only: list[dict[str, Any]] = []
    for finding in findings:
        path = str(finding.get("path") or "").removeprefix("a/").removeprefix("b/")
        line = finding.get("line")
        if path and type(line) is int and line in valid_lines.get(path, set()):
            inline.append({
                "path": path,
                "line": line,
                "side": "RIGHT",
                "body": format_inline_comment(finding),
            })
        else:
            summary_only.append(finding)
    return inline, summary_only


def format_body(
    marker: str,
    head_sha: str,
    review: dict[str, Any],
    provider: str,
    model: str,
    *,
    inline_count: int = 0,
    summary_findings: list[dict[str, Any]] | None = None,
) -> str:
    counts = {severity: 0 for severity in ("P1", "P2", "P3")}
    for finding in review["findings"]:
        counts[finding["severity"]] += 1
    lines = [
        marker,
        "",
        "## Reviewer",
        f"**{'Approved' if review['approve'] else 'Changes requested'}** for current head `{head_sha[:7]}`",
        "",
        review["summary"] or "No summary provided.",
        "",
        f"Findings: **P1 {counts['P1']} · P2 {counts['P2']} · P3 {counts['P3']}**",
        f"Model: `{provider}/{model}`",
        "",
        "### Findings",
        "",
    ]
    displayed = review["findings"] if summary_findings is None else summary_findings
    if inline_count:
        lines.append(f"_{inline_count} finding{' is' if inline_count == 1 else 's are'} attached inline to the changed code._")
        if displayed:
            lines.append("")
    if not review["findings"]:
        lines.append("_No findings._")
    for finding in displayed:
        lines.extend([
            f"- **[{finding['severity']}] `{finding['key']}`** {finding['title']}",
            *( [f"  - {finding['reasoning']}"] if finding["reasoning"] else [] ),
            *( [f"  - _suggestion:_ {finding['suggestion']}"] if finding["suggestion"] else [] ),
        ])
    machine = {
        "version": 1,
        "head_sha": head_sha,
        "verdict": "approve" if review["approve"] else "request_changes",
        "findings": [
            {
                "severity": f["severity"],
                "key": f["key"],
                "title": f["title"],
                "reasoning": f["reasoning"],
                "suggestion": f["suggestion"],
            }
            for f in review["findings"]
        ],
    }
    lines.extend(["", encode_data(machine)])
    return "\n".join(lines) + "\n"


def review_payload(
    marker: str,
    head_sha: str,
    review: dict[str, Any],
    provider: str,
    model: str,
    diff: str,
) -> dict[str, Any]:
    inline, summary_only = partition_findings(review["findings"], diff)
    body = format_body(
        marker,
        head_sha,
        review,
        provider,
        model,
        inline_count=len(inline),
        summary_findings=summary_only,
    )
    payload: dict[str, Any] = {
        "body": body,
        "event": "APPROVE" if review["approve"] else "REQUEST_CHANGES",
        "commit_id": head_sha,
    }
    if inline:
        payload["comments"] = inline
    return payload


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
    candidates = [(provider, model)]
    if config.review.fallback_provider and config.review.fallback_model:
        candidates.append((config.review.fallback_provider, config.review.fallback_model))
    failures: list[str] = []
    for active_provider, active_model in candidates:
        env_name = f"{active_provider.upper()}_API_KEY"
        api_key = os.environ.get(env_name, "") or os.environ.get("MODEL_API_KEY", "")
        try:
            reply = complete(active_provider, active_model, system, user, api_key)
            provider, model = active_provider, active_model
            break
        except ModelError as exc:
            failures.append(f"{active_provider}/{active_model}: {exc}")
    else:
        raise ModelError("all configured review providers failed: " + "; ".join(failures))
    raw = normalize_review(extract_json_reply(reply))
    if omitted:
        raw["approve"] = False
        raw["findings"].insert(0, {
            "severity": "P1", "key": "agent-factory://truncated-diff",
            "title": f"Reviewer input omitted {omitted} bytes", "reasoning": "The full diff was not reviewed.",
            "suggestion": "Split the pull request or raise the configured review limit.",
        })
    payload = json.dumps(review_payload(
        config.review.marker, meta["headRefOid"], raw, provider, model, diff
    ))
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
