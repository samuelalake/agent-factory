"""Read-only, bounded CI diagnostics for a Builder revision.

Logs are untrusted build output, never control-plane instructions. The harness
uses a separate Actions-read token and passes only a sanitized excerpt to Builder.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

_MAX_READ_BYTES = 2_000_000
_MAX_EXCERPT_CHARS = 12_000
_MAX_RUNS = 3
_FAILURES = {"failure", "timed_out", "startup_failure", "action_required"}
_ERROR = re.compile(r"(?i)(\berror\b|\bfailed\b|\bfailure\b|\bexception\b|\be: (?:file:|/)|\bfatal\b)")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SECRET = re.compile(r"(?i)(authorization[\s:=]|(?:token|password|secret|api[_-]?key|private[_-]?key)[\w-]*[\s]*[:=]|-----BEGIN .*PRIVATE KEY)")
_KNOWN_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})\b")


def diagnostic_excerpt(log: str) -> str:
    """Keep error windows, remove common credential-bearing lines, cap prompt size."""
    lines = _ANSI.sub("", log).splitlines()
    selected: set[int] = set()
    for index, line in enumerate(lines):
        if _ERROR.search(line):
            selected.update(range(max(0, index - 2), min(len(lines), index + 5)))
    # A job can fail with no conventional error string. Keep a small tail rather
    # than claiming the diagnostic is empty or dumping the whole job environment.
    if not selected:
        selected.update(range(max(0, len(lines) - 30), len(lines)))
    output = []
    previous = -2
    for index in sorted(selected):
        if index != previous + 1:
            output.append("…")
        line = lines[index]
        output.append("[credential-bearing line omitted]" if _SECRET.search(line)
                      else _KNOWN_TOKEN.sub("[redacted]", line))
        previous = index
    excerpt = "\n".join(output)
    if len(excerpt) > _MAX_EXCERPT_CHARS:
        excerpt = excerpt[:_MAX_EXCERPT_CHARS] + "\n[diagnostic excerpt truncated]"
    return excerpt


def _read(args: list[str], *, root: Path, token: str) -> str:
    # Disk-backed output prevents a verbose failed build from filling memory.
    # Neither raw logs nor credentials are persisted in the consumer checkout.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "TMPDIR", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    env.update(GH_TOKEN=token, GH_PROMPT_DISABLED="1")
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run(["gh", *args], cwd=root, env=env, stdout=output,
                                stderr=errors, timeout=60, check=False)
        if result.returncode:
            # Do not echo arbitrary gh output into the prompt or CI logs.
            raise RuntimeError("CI diagnostics request failed")
        size = output.tell()
        if size > _MAX_READ_BYTES:
            if "--log-failed" not in args:
                raise RuntimeError("CI metadata exceeds diagnostic limit")
            output.seek(-_MAX_READ_BYTES, os.SEEK_END)
        else:
            output.seek(0)
        text = output.read(_MAX_READ_BYTES).decode("utf-8", errors="replace")
        return ("[log tail; earlier output omitted]\n" if size > _MAX_READ_BYTES else "") + text


def collect_ci_failures(repo: str, head: str, *, root: Path, token: str) -> str:
    """Only the latest run per workflow for this repository and exact head counts."""
    if not token:
        return "CI diagnostics unavailable: caller must grant Actions read access and supply the harness CI-read token."
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("CI diagnostics require a repository and full commit SHA")
    try:
        payload = json.loads(_read([
            "api", f"repos/{repo}/actions/runs?head_sha={head}&per_page=50",
        ], root=root, token=token))
        runs = payload["workflow_runs"]
        latest: dict[int, dict] = {}
        for run in runs:
            if (run.get("head_sha") != head
                    or (run.get("head_repository") or {}).get("full_name") != repo
                    or run.get("event") not in {"pull_request", "push", "workflow_dispatch"}):
                continue
            workflow = run.get("workflow_id")
            run_id = run.get("id")
            if type(workflow) is not int or type(run_id) is not int or run_id <= 0:
                continue
            if workflow not in latest or run_id > latest[workflow]["id"]:
                latest[workflow] = run
        failed = sorted((r for r in latest.values() if r.get("conclusion") in _FAILURES
                         and r.get("status") == "completed"), key=lambda r: r["id"], reverse=True)
        reports = []
        for run in failed[:_MAX_RUNS]:
            run_id = run["id"]
            try:
                log = _read(["run", "view", str(run_id), "--repo", repo, "--log-failed"],
                            root=root, token=token)
                excerpt = diagnostic_excerpt(log)
            except (RuntimeError, subprocess.TimeoutExpired):
                excerpt = "Failed-job log unavailable; inspect the linked run."
            reports.append({"run": f"https://github.com/{repo}/actions/runs/{run_id}",
                            "head": head, "conclusion": run["conclusion"], "diagnostic": excerpt})
        if not reports:
            return "No completed failing latest workflow runs found for this head in the bounded scan (up to 50 runs). This does not establish that CI passed."
        return json.dumps({"head": head, "runs": reports,
                           "omitted_failed_runs": max(0, len(failed) - _MAX_RUNS)}, ensure_ascii=False)
    except (RuntimeError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return "CI diagnostics unavailable: could not read current-head workflow metadata. This does not establish that CI passed."
