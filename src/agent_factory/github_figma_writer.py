"""Dedicated, lease-gated Figma Writer phase for an existing Builder pull request."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .config import Config, load_config
from .context import discover_context
from .github_builder import (
    BuilderBlocked,
    _blocked_detail,
    _claude_agent_env,
    _run,
    _run_claude_code_streaming,
    _validate_candidate,
    _workspace_snapshot,
)
from .protocol import encode_data


FIGMA_RESULT_RE = re.compile(
    r"<figma_writer_result>\s*(\{.*?\})\s*</figma_writer_result>",
    flags=re.DOTALL | re.IGNORECASE,
)
ALLOWED_RECORD_PATHS = (
    ".agent-factory/figma-deliveries/",
    "docs/",
    "FIDELITY.md",
    "REGISTRY.md",
)


def _gh(args: list[str], *, cwd: Path, stdin: str | None = None) -> str:
    return _run(["gh", *args], cwd=cwd, stdin=stdin)


def build_prompt(
    config: Config,
    issue: dict[str, Any],
    pr: dict[str, Any],
    root: Path,
) -> str:
    task = f"{issue.get('title', '')}\n{issue.get('body', '')}"
    context = discover_context(root, config.project, task, role="builder")
    documents = "\n\n".join(
        f"## {document.kind}: {document.path}\n\n{document.content}"
        for document in context
    )
    return f"""You are Figma Writer for {config.project.name}.

Builder has already prepared the repository candidate for GitHub issue #{issue['number']} on
pull request #{pr['number']}. You own only the editable Figma delivery phase.

## Immediate canvas handshake

Within five minutes, invoke `mcp__figma__use_figma` to inspect the canonical Figma file identified
by the issue or repository briefing. You may first read only the minimum repository file needed to
locate that file. Do not perform broad repository discovery before this canvas handshake. Continue
the full delivery only after the live file is reachable.

## Issue

{issue.get('title', '')}

{issue.get('body', '')}

## Pull request

{pr.get('title', '')}

{pr.get('body', '')}

## Repository briefing

{documents or 'No configured briefing files were found. Discover the repository before acting.'}

## Contract

- First decide from the canonical issue whether editable Figma is required.
- When required, use the authenticated Figma MCP tools to inspect the live canonical file and
  its current structure before writing. Reuse its components, variables, auto layout, naming,
  and organization. Do not recreate repository-specific rules from memory.
- Make and verify the complete canvas change, including required screen states, prototype flow,
  component usage, and structure. Read the resulting nodes back before finishing.
- Record the exact Figma file URL, node URLs, roles, and verification result in an existing
  issue-specific contract under `docs/` when one exists. Otherwise create
  `.agent-factory/figma-deliveries/issue-{issue['number']}.md`.
- Do not edit product source, tests, generated code, workflows, configuration, or tokens. The
  repository change in this phase is only the durable Figma delivery record.
- If editable Figma is not part of this issue, do not mutate the canvas or repository.
- Do not commit, push, change pull-request state, or expose credentials. The harness does that.
- End with exactly one JSON object wrapped in `<figma_writer_result>` tags. For a completed
  canvas delivery use status `ready`, a concise summary, `file_url`, and a non-empty `node_urls`
  array. For a genuinely inapplicable issue use status `not_applicable`, a concise summary,
  an empty `file_url`, and an empty `node_urls` array.
- If a real blocker prevents faithful Figma delivery, end with `BUILDER_BLOCKED:` followed by
  the concrete blocker instead of claiming completion.
"""


def parse_result(response: str) -> dict[str, Any]:
    if "BUILDER_BLOCKED:" in response:
        raise BuilderBlocked(response.split("BUILDER_BLOCKED:", 1)[1].strip()[:2000])
    match = FIGMA_RESULT_RE.search(response)
    if match is None:
        raise BuilderBlocked("Figma Writer returned no structured result")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise BuilderBlocked("Figma Writer returned invalid result JSON") from exc
    if not isinstance(value, dict):
        raise BuilderBlocked("Figma Writer result must be an object")
    status = value.get("status")
    if status not in {"ready", "not_applicable"}:
        raise BuilderBlocked("Figma Writer result has an unsupported status")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise BuilderBlocked("Figma Writer result requires a summary")
    file_url = value.get("file_url")
    node_urls = value.get("node_urls")
    if not isinstance(file_url, str) or not isinstance(node_urls, list) or any(
        not isinstance(item, str) for item in node_urls
    ):
        raise BuilderBlocked("Figma Writer result has invalid URL fields")
    if status == "ready":
        urls = [file_url, *node_urls]
        if not file_url or not node_urls or any(
            not item.startswith("https://www.figma.com/") for item in urls
        ):
            raise BuilderBlocked("Figma Writer ready result requires Figma file and node URLs")
    elif file_url or node_urls:
        raise BuilderBlocked("inapplicable Figma Writer result cannot include Figma URLs")
    return {
        "status": status,
        "summary": summary.strip()[:1000],
        "file_url": file_url,
        "node_urls": tuple(node_urls),
    }


def _changed_paths(root: Path) -> tuple[str, ...]:
    tracked = _run(["git", "diff", "--name-only"], cwd=root).splitlines()
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=root
    ).splitlines()
    return tuple(dict.fromkeys(path for path in [*tracked, *untracked] if path))


def _validate_record_paths(paths: tuple[str, ...]) -> None:
    disallowed = [
        path for path in paths
        if not any(path == allowed or path.startswith(allowed) for allowed in ALLOWED_RECORD_PATHS)
    ]
    if disallowed:
        raise BuilderBlocked(
            "Figma Writer changed files outside its delivery-record boundary: "
            + ", ".join(disallowed[:10])
        )


def format_issue_status(
    marker: str,
    issue: str,
    state: str,
    detail: str,
    pr_url: str,
    *,
    head: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    data = {
        "version": 1,
        "role": "figma_writer",
        "issue": int(issue),
        "state": state,
        "pull_request": pr_url,
        **({"head": head} if head else {}),
        **(metadata or {}),
    }
    return "\n".join([
        marker,
        "",
        "## Figma Writer",
        f"**{'Canvas delivery complete' if state == 'delivered' else 'Blocked'}**",
        "",
        detail,
        "",
        encode_data(data),
        "",
    ])


def _upsert_issue_comment(
    repo: str, issue: str, marker: str, body: str, *, root: Path
) -> None:
    comments = json.loads(_gh([
        "api", f"repos/{repo}/issues/{issue}/comments", "--paginate", "--slurp",
    ], cwd=root))
    flat = [item for page in comments for item in page] if comments and isinstance(comments[0], list) else comments
    existing = next((item for item in flat if marker in str(item.get("body") or "")), None)
    payload = json.dumps({"body": body})
    if existing and isinstance(existing.get("id"), int):
        endpoint = f"repos/{repo}/issues/comments/{existing['id']}"
        _gh(["api", endpoint, "-X", "PATCH", "--input", "-"], cwd=root, stdin=payload)
    else:
        endpoint = f"repos/{repo}/issues/{issue}/comments"
        _gh(["api", endpoint, "-X", "POST", "--input", "-"], cwd=root, stdin=payload)


def run(repo: str, pr_number: str, issue_number: str, root: Path, config_path: Path) -> str:
    if not os.environ.get("GH_TOKEN"):
        raise RuntimeError("GH_TOKEN must be a Builder App installation token")
    config = load_config(config_path)
    if not config.figma.enabled:
        raise RuntimeError("Figma Writer is not enabled")
    issue = json.loads(_gh([
        "issue", "view", issue_number, "--repo", repo, "--json", "number,title,body,state",
    ], cwd=root))
    pr = json.loads(_gh([
        "pr", "view", pr_number, "--repo", repo,
        "--json", "number,title,body,url,headRefName,headRefOid,isDraft",
    ], cwd=root))
    expected_branch = f"{config.builder.branch_prefix}{issue_number}"
    if str(pr.get("headRefName") or "") != expected_branch:
        raise RuntimeError("Figma Writer received a pull request outside the Builder branch")
    if not bool(pr.get("isDraft")):
        raise RuntimeError("Figma Writer requires a draft pull request")
    expected_head = str(pr.get("headRefOid") or "")
    if _run(["git", "rev-parse", "HEAD"], cwd=root).strip() != expected_head:
        raise RuntimeError("Figma Writer checkout does not match the pull request head")

    _run(["git", "config", "user.name", "Agent Factory Figma Writer"], cwd=root)
    _run([
        "git", "config", "user.email",
        "agent-factory-builder[bot]@users.noreply.github.com",
    ], cwd=root)
    baseline = _workspace_snapshot(root)
    response, _ = _run_claude_code_streaming(
        build_prompt(config, issue, pr, root),
        root=root,
        model=config.figma.model,
        timeout_seconds=config.figma.timeout_seconds,
        required_tool="mcp__figma__use_figma",
        first_tool_timeout_seconds=300,
        allow_bash=False,
    )
    result = parse_result(response)
    paths = _changed_paths(root)
    if result["status"] == "ready":
        if not paths:
            raise BuilderBlocked("Figma Writer completed without a durable delivery record")
        _validate_record_paths(paths)
        _validate_candidate(root, expected_head, baseline=baseline)
        _run(["git", "add", "--all"], cwd=root)
        _run(["git", "commit", "-m", f"docs: record Figma delivery for issue #{issue_number}"], cwd=root)
        _run(["gh", "auth", "setup-git"], cwd=root)
        _run([
            "git", "push",
            f"--force-with-lease=refs/heads/{expected_branch}:{expected_head}",
            "origin", f"HEAD:{expected_branch}",
        ], cwd=root)
    elif paths:
        raise BuilderBlocked("inapplicable Figma Writer result modified the repository")

    head = _run(["git", "rev-parse", "HEAD"], cwd=root).strip()
    detail = result["summary"]
    if result["status"] == "ready":
        detail += " " + " ".join(result["node_urls"])
    body = format_issue_status(
        config.figma.marker,
        issue_number,
        "delivered",
        detail,
        str(pr["url"]),
        head=head,
    )
    _upsert_issue_comment(repo, issue_number, config.figma.marker, body, root=root)
    print(pr["url"])
    return str(pr["url"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.repo, args.pr, args.issue, args.root, args.config)
    except (BuilderBlocked, RuntimeError, subprocess.TimeoutExpired) as exc:
        config = load_config(args.config)
        detail = _blocked_detail(str(exc), "claude-code", None)
        try:
            pr = json.loads(_gh([
                "pr", "view", args.pr, "--repo", args.repo, "--json", "url",
            ], cwd=args.root))
            body = format_issue_status(
                config.figma.marker,
                args.issue,
                "blocked",
                detail,
                str(pr.get("url") or ""),
            )
            _upsert_issue_comment(
                args.repo, args.issue, config.figma.marker, body, root=args.root
            )
            _gh([
                "label", "create", "agent:steward", "--repo", args.repo,
                "--color", "8250DF", "--description", "Builder needs Steward routing",
                "--force",
            ], cwd=args.root)
            _gh([
                "issue", "edit", args.issue, "--repo", args.repo,
                "--add-label", "agent:steward",
            ], cwd=args.root)
        except RuntimeError:
            pass
        print(f"blocked: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
