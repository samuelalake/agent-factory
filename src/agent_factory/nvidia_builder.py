"""Bounded OpenAI-compatible Builder loop for hosted model providers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
import urllib.error
import urllib.request


ENDPOINTS = {
    "minimax": "https://api.minimax.io/v1/chat/completions",
    "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}
API_KEY_ENV = {
    "minimax": "MINIMAX_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
ENDPOINT = ENDPOINTS["nvidia"]
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
_BLOCKED_COMMANDS = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo|gh|ssh|scp)\b|git\s+push\b|rm\s+-[A-Za-z]*r[A-Za-z]*f\b|"
    r"(?:printenv|env)\b|/proc/(?:self|[0-9]+)/environ",
    re.IGNORECASE,
)


class NvidiaBuilderError(RuntimeError):
    pass


def _inside(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise NvidiaBuilderError("tool path must be repository-relative")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise NvidiaBuilderError("tool path escaped repository") from exc
    if ".git" in path.relative_to(root.resolve()).parts:
        raise NvidiaBuilderError("tools cannot access .git")
    return path


def _tool_env() -> dict[str, str]:
    allowed = {"CI", "HOME", "LANG", "LC_ALL", "PATH", "RUNNER_ARCH", "RUNNER_OS", "TMPDIR"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def _execute_tool(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "list_files":
        pattern = str(arguments.get("pattern") or "*")
        result = subprocess.run(
            ["git", "ls-files", pattern], cwd=root, text=True, capture_output=True, timeout=30
        )
        return {"files": result.stdout.splitlines()[:1000], "truncated": len(result.stdout.splitlines()) > 1000}
    if name == "read_file":
        relative = str(arguments.get("path") or "")
        path = _inside(root, relative)
        if not path.is_file():
            raise NvidiaBuilderError(f"file not found: {relative}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(arguments.get("start_line") or 1))
        end = min(len(lines), int(arguments.get("end_line") or start + 399))
        return {"path": relative, "start_line": start, "end_line": end, "content": "\n".join(lines[start - 1:end])}
    if name == "search":
        pattern = str(arguments.get("pattern") or "")
        relative = str(arguments.get("path") or ".")
        target = _inside(root, relative)
        result = subprocess.run(
            ["rg", "-n", "--max-count", "200", "--", pattern, str(target)],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30,
        )
        return {"matches": (result.stdout or result.stderr)[-20000:], "exit_code": result.returncode}
    if name == "write_file":
        relative = str(arguments.get("path") or "")
        content = str(arguments.get("content") or "")
        if len(content.encode()) > 500_000:
            raise NvidiaBuilderError("write exceeds 500000-byte tool limit")
        path = _inside(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": relative, "bytes": len(content.encode())}
    if name == "replace_text":
        relative = str(arguments.get("path") or "")
        old = str(arguments.get("old") or "")
        new = str(arguments.get("new") or "")
        path = _inside(root, relative)
        content = path.read_text(encoding="utf-8")
        count = content.count(old)
        if not old or count != 1:
            raise NvidiaBuilderError(f"replace_text requires exactly one match; found {count}")
        path.write_text(content.replace(old, new, 1), encoding="utf-8")
        return {"path": relative, "replacements": 1}
    if name == "run_command":
        command = str(arguments.get("command") or "").strip()
        if not command or len(command) > 4000 or _BLOCKED_COMMANDS.search(command):
            raise NvidiaBuilderError("command rejected by Builder policy")
        result = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=root,
            env=_tool_env(),
            text=True,
            capture_output=True,
            timeout=300,
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout[-20000:],
            "stderr": result.stderr[-10000:],
        }
    raise NvidiaBuilderError(f"unknown tool: {name}")


def _tools() -> list[dict[str, Any]]:
    path = {"type": "string", "description": "Repository-relative path"}
    return [
        {"type": "function", "function": {"name": "list_files", "description": "List tracked repository files by glob.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "read_file", "description": "Read a bounded range from a repository file.", "parameters": {"type": "object", "properties": {"path": path, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}}},
        {"type": "function", "function": {"name": "search", "description": "Search repository text with ripgrep.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": path}, "required": ["pattern"]}}},
        {"type": "function", "function": {"name": "write_file", "description": "Create or replace a repository text file.", "parameters": {"type": "object", "properties": {"path": path, "content": {"type": "string"}}, "required": ["path", "content"]}}},
        {"type": "function", "function": {"name": "replace_text", "description": "Replace one exact text occurrence in a repository file.", "parameters": {"type": "object", "properties": {"path": path, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}}},
        {"type": "function", "function": {"name": "run_command", "description": "Run a bounded repository build, test, inspection, or generation command. Publishing and credential commands are denied.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    ]


def _post(
    model: str,
    messages: list[dict[str, Any]],
    api_key: str,
    timeout: int,
    *,
    provider: str = "nvidia",
) -> dict[str, Any]:
    endpoint = ENDPOINTS.get(provider)
    if endpoint is None:
        raise NvidiaBuilderError(f"unsupported OpenAI-compatible provider: {provider}")
    payload = {
        "model": model,
        "messages": messages,
        "tools": _tools(),
        "tool_choice": "auto",
        "max_tokens": 4096,
        "temperature": 0.2,
        "stream": False,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            if exc.code not in TRANSIENT_HTTP_CODES or attempt == 2:
                raise NvidiaBuilderError(f"{provider} HTTP {exc.code}: {detail}") from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = int(float(retry_after)) if retry_after else 30 * (attempt + 1)
            except ValueError:
                delay = 30 * (attempt + 1)
            time.sleep(max(1, min(delay, 60)))
        except urllib.error.URLError as exc:
            raise NvidiaBuilderError(f"{provider} endpoint unreachable: {exc.reason}") from exc
    raise NvidiaBuilderError(f"{provider} retry loop exhausted")


def run_openai_builder(
    prompt: str,
    root: Path,
    *,
    provider: str,
    model: str,
    api_key: str,
    max_requests: int,
    timeout_seconds: int,
    max_cost_usd: float,
    input_cost_per_million: float,
    output_cost_per_million: float,
) -> tuple[str, int, float]:
    secret_name = API_KEY_ENV.get(provider)
    if secret_name is None:
        raise NvidiaBuilderError(f"unsupported OpenAI-compatible provider: {provider}")
    if not api_key:
        raise NvidiaBuilderError(f"{secret_name} is unavailable")
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    deadline = time.monotonic() + timeout_seconds
    tool_count = 0
    prompt_tokens = 0
    completion_tokens = 0
    for _ in range(max_requests):
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            raise NvidiaBuilderError(f"{provider} Builder exceeded its time budget")
        response = _post(
            model, messages, api_key, min(300, remaining), provider=provider
        )
        usage = response.get("usage") or {}
        if (input_cost_per_million or output_cost_per_million) and not usage:
            raise NvidiaBuilderError(
                f"{provider} omitted token usage required for cost enforcement"
            )
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)
        estimated_cost = (
            prompt_tokens * input_cost_per_million
            + completion_tokens * output_cost_per_million
        ) / 1_000_000
        if estimated_cost > max_cost_usd:
            raise NvidiaBuilderError(
                f"{provider} Builder exceeded its ${max_cost_usd:.2f} estimated cost limit"
            )
        choices = response.get("choices") or []
        if not choices:
            raise NvidiaBuilderError(f"{provider} returned no choices")
        assistant = choices[0].get("message") or {}
        messages.append(assistant)
        calls = assistant.get("tool_calls") or []
        if not calls:
            if tool_count < 1:
                raise NvidiaBuilderError(
                    f"{provider} Builder returned without using repository tools"
                )
            return str(assistant.get("content") or ""), tool_count, estimated_cost
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise NvidiaBuilderError("tool arguments must be an object")
                result = {"ok": True, "result": _execute_tool(root, name, arguments)}
            except (ValueError, OSError, subprocess.SubprocessError, NvidiaBuilderError) as exc:
                result = {"ok": False, "error": str(exc)[:2000]}
            tool_count += 1
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(result),
                }
            )
    raise NvidiaBuilderError(f"{provider} Builder exceeded {max_requests} model requests")


def run_nvidia_builder(
    prompt: str,
    root: Path,
    *,
    model: str,
    api_key: str,
    max_requests: int,
    timeout_seconds: int,
) -> tuple[str, int]:
    response, tool_count, _ = run_openai_builder(
        prompt,
        root,
        provider="nvidia",
        model=model,
        api_key=api_key,
        max_requests=max_requests,
        timeout_seconds=timeout_seconds,
        max_cost_usd=float("inf"),
        input_cost_per_million=0,
        output_cost_per_million=0,
    )
    return response, tool_count
