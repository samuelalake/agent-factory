"""Agentic Builder harness and GitHub publication adapter."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from .config import Config, load_config
from .context import discover_context
from .protocol import encode_data
from .nvidia_builder import API_KEY_ENV, NvidiaBuilderError, run_openai_builder


class BuilderBlocked(RuntimeError):
    pass


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    stdin: str | None = None,
) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip())[-4000:]
        raise RuntimeError(detail or f"command failed: {args[0]}")
    return result.stdout


def _gh(args: list[str], *, cwd: Path, stdin: str | None = None) -> str:
    return _run(["gh", *args], cwd=cwd, stdin=stdin)


def _safe_agent_env() -> dict[str, str]:
    allowed = {
        "CI", "HOME", "LANG", "LC_ALL", "PATH", "RUNNER_ARCH", "RUNNER_OS",
        "TMPDIR", "XCODE_VERSION_ACTUAL",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "")
    env["GEMINI_SANDBOX"] = "false"
    # Hosted runners are disposable checkouts. Gemini CLI otherwise downgrades
    # YOLO to interactive approval and refuses to run in headless CI.
    env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    return env


def _clean_detail(value: str) -> str:
    """Keep issue status concise and free of terminal control sequences."""
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    lines = list(dict.fromkeys(line.strip() for line in clean.splitlines() if line.strip()))
    return "\n".join(lines)[-2000:]


def _preserve_workflow_control_plane(root: Path) -> tuple[str, ...]:
    """Discard candidate edits to the workflows that execute Builder credentials."""
    workflow_root = ".github/workflows"
    changed = {
        item
        for item in _run(
            ["git", "diff", "--name-only", "--cached", "-z", "--", workflow_root],
            cwd=root,
        ).split("\0")
        if item
    }
    changed.update(
        item
        for item in _run(
            ["git", "diff", "--name-only", "-z", "--", workflow_root], cwd=root
        ).split("\0")
        if item
    )
    tracked = _run(["git", "ls-files", "-z", "--", workflow_root], cwd=root)
    if tracked and changed:
        _run(
            ["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", workflow_root],
            cwd=root,
        )

    untracked = tuple(
        item
        for item in _run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", workflow_root],
            cwd=root,
        ).split("\0")
        if item
    )
    for relative in untracked:
        target = (root / relative).resolve()
        try:
            target.relative_to((root / workflow_root).resolve())
        except ValueError as exc:
            raise BuilderBlocked(f"unsafe workflow path from Builder: {relative}") from exc
        if target.is_file() or target.is_symlink():
            target.unlink()
        else:
            raise BuilderBlocked(f"cannot preserve workflow control-plane path: {relative}")
    return tuple(sorted(changed | set(untracked)))


def _validate_candidate(root: Path) -> tuple[str, ...]:
    preserved = _preserve_workflow_control_plane(root)
    if not _run(["git", "status", "--porcelain"], cwd=root).strip():
        if preserved:
            raise BuilderBlocked(
                "Builder changed only protected GitHub workflows; the control plane was preserved"
            )
        raise BuilderBlocked("Builder produced no repository changes")
    return preserved


def _review_feedback(repo: str, pr: int, head: str, marker: str, *, root: Path) -> str:
    reviews = json.loads(
        _gh(["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate"], cwd=root)
    )
    current = [
        item
        for item in reviews
        if str(item.get("commit_id") or "") == head
        and marker in str(item.get("body") or "")
        and str(item.get("state") or "").upper() == "CHANGES_REQUESTED"
    ]
    if not current:
        return ""
    body = str(current[-1].get("body") or "")
    return body.split("<!-- agent-factory:data", 1)[0].strip()[-8000:]


def _builder_summary(response: str, issue_number: str) -> str:
    summary = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL | re.IGNORECASE)
    summary = re.sub(r"<analysis>.*?</analysis>", "", summary, flags=re.DOTALL | re.IGNORECASE)
    summary = summary.strip()
    self_talk = re.compile(r"^(let me|now i|i need|i'm (?:going|trying)|next i)\b", re.IGNORECASE)
    if len(summary) < 80 or summary.endswith(":") or self_talk.search(summary):
        return (
            f"Builder completed an implementation pass for issue #{issue_number} using "
            "repository tools. Reviewer and repository workflows will validate the current head."
        )
    return summary[-3000:]


def parse_gemini_stream(output: str) -> tuple[str, int]:
    messages: list[str] = []
    tool_calls = 0
    result_status = ""
    for line in output.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        event_type = str(event.get("type") or "")
        if event_type == "tool_use":
            tool_calls += 1
        elif event_type == "message" and event.get("role") == "assistant":
            content = str(event.get("content") or "")
            if content:
                messages.append(content)
        elif event_type == "result":
            result_status = str(event.get("status") or "")
    if result_status and result_status != "success":
        raise BuilderBlocked(f"Gemini CLI result was {result_status}")
    if tool_calls < 1:
        raise BuilderBlocked("Gemini CLI completed without using repository tools")
    return (messages[-1] if messages else ""), tool_calls


def _quota_delay(detail: str) -> int | None:
    """Return a bounded server-requested delay for transient quota failures."""
    if "429" not in detail and "quota exceeded" not in detail.lower():
        return None
    match = re.search(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", detail, re.IGNORECASE)
    requested = int(float(match.group(1))) + 2 if match else 60
    return max(5, min(requested, 90))


def _run_gemini(prompt: str, *, root: Path, model: str, timeout_seconds: int) -> str:
    """Run Gemini once, then resume the saved session across transient 429s."""
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    for attempt in range(3):
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            break
        if attempt == 0:
            task_args = ["--prompt", prompt]
        else:
            task_args = [
                "--resume", "latest", "--prompt",
                "Continue the assigned issue from the saved session. Finish the implementation and verification.",
            ]
        try:
            return _run(
                [
                    "gemini", "--model", model,
                    "--approval-mode", "yolo", "--output-format", "stream-json",
                    *task_args,
                ],
                cwd=root,
                env=_safe_agent_env(),
                timeout=remaining,
            )
        except RuntimeError as exc:
            last_error = exc
            delay = _quota_delay(str(exc))
            if delay is None or attempt == 2 or delay >= remaining:
                raise
            time.sleep(delay)
    if last_error:
        raise last_error
    raise subprocess.TimeoutExpired("gemini", timeout_seconds)


def build_prompt(
    config: Config,
    issue: dict[str, Any],
    root: Path,
    review_feedback: str = "",
) -> str:
    task = f"{issue.get('title', '')}\n{issue.get('body', '')}"
    context = discover_context(root, config.project, task, role="builder")
    documents = "\n\n".join(
        f"## {document.kind}: {document.path}\n\n{document.content}" for document in context
    )
    feedback = (
        f"""
## Current-head Reviewer feedback

This is a revision of an existing pull request. Resolve every finding below; do not merely
describe it. Re-run relevant verification and keep unrelated valid work intact.

{review_feedback}
"""
        if review_feedback
        else ""
    )
    return f"""You are Builder for {config.project.name}.

Implement GitHub issue #{issue['number']} completely in the current checkout.

## Issue

{issue.get('title', '')}

{issue.get('body', '')}

## Repository briefing

{documents or 'No configured briefing files were found. Discover the repository before acting.'}
{feedback}

## Contract

- Begin from the current checkout, which Steward prepared from {config.builder.base_branch}.
- Discover and follow repository instructions, relevant skills, history, and existing conventions.
- Do not use operator-authored implementation branches or unrelated pull requests as implementation input.
- Inspect source artifacts and run repository tools on this runner; do not invent values or weaken acceptance criteria.
- Implement the issue, run proportionate tests, and leave the complete working-tree changes in place.
- Do not edit `.github/workflows/**`; those workflows are a protected control plane managed separately.
- Do not commit, push, open a pull request, merge, or expose credentials. The harness performs publication.
- If a real blocker prevents faithful completion, make no placeholder implementation and end your response with `BUILDER_BLOCKED:` followed by the concrete blocker.
"""


def format_issue_status(marker: str, issue: str, state: str, detail: str, pr_url: str = "") -> str:
    machine = {
        "version": 1,
        "role": "builder",
        "issue": int(issue),
        "state": state,
        **({"pull_request": pr_url} if pr_url else {}),
    }
    headline = "Pull request delivered" if state == "delivered" else "Blocked"
    return "\n".join(
        [
            marker,
            "",
            "## Builder",
            f"**{headline}**",
            "",
            detail,
            "",
            encode_data(machine),
            "",
        ]
    )


def _upsert_issue_comment(repo: str, issue: str, marker: str, body: str, *, root: Path) -> None:
    pages = json.loads(
        _gh(
            ["api", f"repos/{repo}/issues/{issue}/comments", "--paginate", "--slurp"],
            cwd=root,
        )
    )
    comments = [item for page in pages for item in page] if pages and isinstance(pages[0], list) else pages
    existing = next(
        (item for item in comments if marker in str(item.get("body") or "")), None
    )
    payload = json.dumps({"body": body})
    if existing and isinstance(existing.get("id"), int):
        _gh(
            ["api", f"repos/{repo}/issues/comments/{existing['id']}", "-X", "PATCH", "--input", "-"],
            cwd=root,
            stdin=payload,
        )
    else:
        _gh(
            ["api", f"repos/{repo}/issues/{issue}/comments", "-X", "POST", "--input", "-"],
            cwd=root,
            stdin=payload,
        )


def run(repo: str, issue_number: str, root: Path, config_path: Path) -> str:
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise RuntimeError("GH_TOKEN must be a Builder App installation token")
    config = load_config(config_path)
    issue = json.loads(
        _gh(
            ["issue", "view", issue_number, "--repo", repo, "--json", "number,title,body,state"],
            cwd=root,
        )
    )
    if str(issue.get("state") or "").upper() != "OPEN":
        raise RuntimeError("Builder only accepts open issues")

    branch = f"{config.builder.branch_prefix}{issue_number}"
    existing = json.loads(
        _gh(
            [
                "pr", "list", "--repo", repo, "--state", "open", "--head", branch,
                "--json", "number,url,headRefOid",
            ],
            cwd=root,
        )
    )
    feedback = ""
    if existing:
        feedback = _review_feedback(
            repo,
            int(existing[0]["number"]),
            str(existing[0].get("headRefOid") or ""),
            config.review.marker,
            root=root,
        )
        _run(["gh", "auth", "setup-git"], cwd=root)
        _run(["git", "fetch", "origin", branch], cwd=root)
        start_ref = "FETCH_HEAD"
    else:
        start_ref = f"origin/{config.builder.base_branch}"
    _run(["git", "checkout", "-B", branch, start_ref], cwd=root)
    prompt = build_prompt(config, issue, root, feedback)
    harness = config.builder.harness
    model = config.builder.model
    estimated_cost: float | None = None

    def run_compatible(provider: str, selected_model: str) -> tuple[str, int, float]:
        secret_name = API_KEY_ENV.get(provider)
        if secret_name is None:
            raise NvidiaBuilderError(f"unsupported Builder provider: {provider}")
        return run_openai_builder(
            prompt,
            root,
            provider=provider,
            model=selected_model,
            api_key=os.environ.get(secret_name, ""),
            max_requests=config.builder.max_model_requests,
            timeout_seconds=config.builder.timeout_seconds,
            max_cost_usd=config.builder.max_model_cost_usd,
            input_cost_per_million=config.builder.input_cost_per_million,
            output_cost_per_million=config.builder.output_cost_per_million,
        )

    try:
        if config.builder.provider == "gemini":
            if config.builder.harness != "gemini-cli":
                raise RuntimeError(f"unsupported Gemini harness: {config.builder.harness}")
            output = _run_gemini(
                prompt,
                root=root,
                model=config.builder.model,
                timeout_seconds=config.builder.timeout_seconds,
            )
            response, tool_calls = parse_gemini_stream(output)
        else:
            response, tool_calls, estimated_cost = run_compatible(
                config.builder.provider, config.builder.model
            )
            harness = "openai-compatible-tool-loop"
        _validate_candidate(root)
    except (
        BuilderBlocked,
        RuntimeError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        NvidiaBuilderError,
    ) as exc:
        if not config.builder.fallback_provider or not config.builder.fallback_model:
            raise BuilderBlocked(f"{config.builder.provider} Builder failed: {exc}") from exc
        if _run(["git", "status", "--porcelain"], cwd=root).strip():
            raise BuilderBlocked(
                f"{config.builder.provider} Builder failed after modifying the workspace; "
                f"fallback was not mixed into partial work: {exc}"
            ) from exc
        try:
            response, tool_calls, estimated_cost = run_compatible(
                config.builder.fallback_provider, config.builder.fallback_model
            )
            harness = "openai-compatible-tool-loop"
            model = config.builder.fallback_model
            _validate_candidate(root)
        except (BuilderBlocked, NvidiaBuilderError) as fallback_exc:
            raise BuilderBlocked(
                f"{config.builder.provider} Builder failed: {exc}; "
                f"{config.builder.fallback_provider} fallback failed: {fallback_exc}"
            ) from fallback_exc

    if "BUILDER_BLOCKED:" in response:
        raise BuilderBlocked(response.split("BUILDER_BLOCKED:", 1)[1].strip()[:2000])
    _run(["git", "config", "user.name", "Agent Factory Builder"], cwd=root)
    _run(["git", "config", "user.email", "agent-factory-builder[bot]@users.noreply.github.com"], cwd=root)
    _run(["git", "add", "--all"], cwd=root)
    _run(["git", "commit", "-m", f"feat: implement issue #{issue_number}"], cwd=root)
    _run(["gh", "auth", "setup-git"], cwd=root)
    _run(["git", "push", "--force-with-lease", "origin", branch], cwd=root)

    if existing:
        pr_url = str(existing[0]["url"])
    else:
        pr_body = "\n".join(
            [
                config.builder.marker,
                "",
                f"Closes #{issue_number}",
                "",
                "## Builder summary",
                "",
                _builder_summary(response, issue_number),
                "",
                "## Delivery",
                "",
                f"- Base: `{config.builder.base_branch}`",
                f"- Harness: `{harness}`",
                f"- Model: `{model}`",
                f"- Repository tool calls: `{tool_calls}`",
                f"- Estimated model cost: `{f'${estimated_cost:.4f}' if estimated_cost is not None else 'provider reported separately'}`",
                "- Verification: repository workflows run on this pull request",
                "",
            ]
        )
        pr_url = _gh(
            [
                "pr", "create", "--repo", repo, "--base", config.builder.base_branch,
                "--head", branch, "--title", str(issue["title"]), "--body-file", "-",
            ],
            cwd=root,
            stdin=pr_body,
        ).strip()

    status = format_issue_status(
        config.builder.marker,
        issue_number,
        "delivered",
        f"Builder opened or updated {pr_url}. Reviewer and repository verification own the next decision.",
        pr_url,
    )
    _upsert_issue_comment(repo, issue_number, config.builder.marker, status, root=root)
    _gh(["issue", "edit", issue_number, "--repo", repo, "--remove-label", config.steward.dispatch_label], cwd=root)
    print(pr_url)
    return pr_url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.repo, args.issue, args.root, args.config)
    except (BuilderBlocked, RuntimeError, subprocess.TimeoutExpired) as exc:
        config = load_config(args.config)
        detail = _clean_detail(str(exc))
        body = format_issue_status(config.builder.marker, args.issue, "blocked", detail)
        _upsert_issue_comment(args.repo, args.issue, config.builder.marker, body, root=args.root)
        _gh(
            [
                "label", "create", "agent:steward", "--repo", args.repo,
                "--color", "8250DF", "--description", "Builder needs Steward routing",
                "--force",
            ],
            cwd=args.root,
        )
        _gh(["issue", "edit", args.issue, "--repo", args.repo, "--add-label", "agent:steward"], cwd=args.root)
        try:
            _gh(
                ["issue", "edit", args.issue, "--repo", args.repo, "--remove-label", config.steward.dispatch_label],
                cwd=args.root,
            )
        except RuntimeError:
            pass
        print(f"blocked: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
