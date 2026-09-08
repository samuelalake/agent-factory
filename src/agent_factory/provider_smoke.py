"""Small, secret-safe smoke test for OpenAI-compatible Builder tool loops."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any
import urllib.error
import urllib.request

from .nvidia_builder import API_KEY_ENV, ENDPOINTS, _tools


PREFERRED_MODELS = (
    "moonshotai/kimi-k3",
    "laguna-xs-2.1",
    "glm-5.2",
    "minimax-m3",
    "qwen3-coder",
    "qwen2.5",
    "llama-3.3",
)

MAX_SMOKE_CANDIDATES = 8
MAX_SMOKE_OUTPUT_TOKENS = 8_192
MAX_SMOKE_PROMPT_BYTES = 200_000
PROBE_NONCE = "agent-factory-smoke-v1"
PROBE_ACK = f"PROBE_COMPLETE {PROBE_NONCE}"
PROBE_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKElEQVR4nO3NsQ0AAAzCMP5/un0CNkuZ41wybXsHAAAAAAAAAAAAxR4yw/wuPL6QkAAAAABJRU5ErkJggg=="
)


class ProviderSmokeError(RuntimeError):
    pass


def _request(url: str, api_key: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            try:
                value = json.load(response)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProviderSmokeError("provider returned invalid JSON") from exc
            if not isinstance(value, dict):
                raise ProviderSmokeError("provider returned an invalid response shape")
            return value
    except urllib.error.HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        remaining = None
        reset = None
        if exc.headers:
            remaining = exc.headers.get("x-ratelimit-remaining")
            reset = exc.headers.get("x-ratelimit-reset")
        # Provider response bodies are arbitrary and can reflect request content or
        # credentials. Discard them and publish only allowlisted rate metadata.
        exc.read(8_192)
        metadata = ", ".join(
            f"{name}={value}"
            for name, value in (
                ("retry_after", retry_after),
                ("remaining", remaining),
                ("reset", reset),
            )
            if value is not None
        )
        suffix = f" ({metadata})" if metadata else ""
        raise ProviderSmokeError(f"HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise ProviderSmokeError("endpoint unreachable") from exc


def discover_models(provider: str, api_key: str) -> list[str]:
    endpoint = ENDPOINTS.get(provider)
    if endpoint is None:
        raise ProviderSmokeError(f"unsupported provider: {provider}")
    catalog = _request(endpoint.removesuffix("/chat/completions") + "/models", api_key)
    items = catalog.get("data")
    if not isinstance(items, list):
        raise ProviderSmokeError("provider returned an invalid model catalog")
    models: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ProviderSmokeError("provider returned an invalid model catalog")
        model_id = item.get("id")
        if model_id is not None and not isinstance(model_id, str):
            raise ProviderSmokeError("provider returned an invalid model catalog")
        if model_id:
            models.append(model_id)
    return models


def select_candidates(catalog: list[str], requested: list[str], limit: int) -> list[str]:
    selected: list[str] = []
    for model in requested:
        if model and model not in selected:
            selected.append(model)
    lowered = [(model, model.lower()) for model in catalog]
    for preference in PREFERRED_MODELS:
        for model, normalized in lowered:
            if preference in normalized and model not in selected:
                selected.append(model)
                break
    return selected[:limit]


def probe_model(
    provider: str,
    model: str,
    api_key: str,
    *,
    builder_shape: bool = False,
    max_output_tokens: int = 128,
    prompt_bytes: int = 0,
    visual_input: bool = False,
) -> None:
    if not 1 <= max_output_tokens <= MAX_SMOKE_OUTPUT_TOKENS:
        raise ProviderSmokeError(
            f"max_output_tokens must be from 1 through {MAX_SMOKE_OUTPUT_TOKENS}"
        )
    if not 0 <= prompt_bytes <= MAX_SMOKE_PROMPT_BYTES:
        raise ProviderSmokeError(
            f"prompt_bytes must be from 0 through {MAX_SMOKE_PROMPT_BYTES}"
        )
    endpoint = ENDPOINTS[provider]
    minimal_tool = {
        "type": "function",
        "function": {
            "name": "write_probe",
            "description": "Record the exact smoke-test value.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        },
    }
    tools = _tools() if builder_shape else [minimal_tool]
    expected_tool = "list_files" if builder_shape else "write_probe"
    if visual_input:
        requested_arguments = {"pattern": "red"} if builder_shape else {"value": "red"}
        instruction = (
            f"Inspect the attached single-color image. Call {expected_tool} exactly once "
            "with its string argument equal to the lowercase color name visible in the image. "
            "Do not answer in prose first. After the tool result, reply exactly "
            "PROBE_COMPLETE followed by its nonce."
        )
    else:
        requested_arguments = {"pattern": "*"} if builder_shape else {"value": "ready"}
        instruction = (
            "Call list_files exactly once with pattern *. Do not answer in prose first. "
            f"After the tool result, reply exactly PROBE_COMPLETE followed by its nonce."
            if builder_shape
            else "Call write_probe exactly once with value ready. Do not answer in prose first. "
            f"After the tool result, reply exactly PROBE_COMPLETE followed by its nonce."
        )
    if prompt_bytes > len(instruction.encode()):
        instruction += "\nContext padding:\n" + ("x" * (prompt_bytes - len(instruction.encode())))
    content: str | list[dict[str, Any]] = instruction
    if visual_input:
        content = [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": PROBE_IMAGE}},
        ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": content,
        }
    ]
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "required",
        "max_tokens": max_output_tokens,
        "temperature": 0,
        "stream": False,
    }
    if provider == "minimax":
        # Match the production Builder loop: MiniMax requires its reasoning
        # state to be preserved across tool turns, but the final content must
        # remain clean enough for an exact acknowledgement.
        payload["reasoning_split"] = True
    first = _request(endpoint, api_key, payload=payload)
    choices = first.get("choices") or []
    first_choice = choices[0] if isinstance(choices, list) and choices else None
    assistant = first_choice.get("message") if isinstance(first_choice, dict) else None
    if not isinstance(assistant, dict):
        raise ProviderSmokeError("provider returned an invalid chat response")
    calls = assistant.get("tool_calls") or []
    if not isinstance(calls, list):
        raise ProviderSmokeError("provider returned an invalid chat response")
    call = calls[0] if len(calls) == 1 else None
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        raise ProviderSmokeError("model did not produce the required tool call")
    if function.get("name") != expected_tool:
        raise ProviderSmokeError("model did not produce the required tool call")
    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        raise ProviderSmokeError("model tool call did not include an id")
    raw_arguments = function.get("arguments")
    if not isinstance(raw_arguments, str):
        raise ProviderSmokeError("model tool call arguments were invalid JSON")
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProviderSmokeError("model tool call arguments were invalid JSON") from exc
    if arguments != requested_arguments:
        raise ProviderSmokeError("tool call arguments did not preserve the requested value")
    messages.extend(
        [
            assistant,
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": expected_tool,
                "content": json.dumps(
                    {"ok": True, "result": requested_arguments, "nonce": PROBE_NONCE}
                ),
            },
        ]
    )
    second_payload = {**payload, "messages": messages, "tool_choice": "auto"}
    second = _request(endpoint, api_key, payload=second_payload)
    second_choices = second.get("choices") or []
    second_choice = (
        second_choices[0]
        if isinstance(second_choices, list) and len(second_choices) == 1
        else None
    )
    second_message = (
        second_choice.get("message") if isinstance(second_choice, dict) else None
    )
    if (
        not isinstance(second_message, dict)
        or second_message.get("tool_calls")
        or str(second_message.get("content") or "").strip() != PROBE_ACK
    ):
        raise ProviderSmokeError("model did not acknowledge the tool result")


def run(
    provider: str,
    requested: list[str],
    *,
    discover: bool,
    limit: int,
    builder_shape: bool = False,
    max_output_tokens: int = 128,
    prompt_bytes: int = 0,
    visual_input: bool = False,
) -> str:
    if not 1 <= limit <= MAX_SMOKE_CANDIDATES:
        raise ProviderSmokeError(
            f"limit must be from 1 through {MAX_SMOKE_CANDIDATES}"
        )
    if not 1 <= max_output_tokens <= MAX_SMOKE_OUTPUT_TOKENS:
        raise ProviderSmokeError(
            f"max_output_tokens must be from 1 through {MAX_SMOKE_OUTPUT_TOKENS}"
        )
    if not 0 <= prompt_bytes <= MAX_SMOKE_PROMPT_BYTES:
        raise ProviderSmokeError(
            f"prompt_bytes must be from 0 through {MAX_SMOKE_PROMPT_BYTES}"
        )
    secret_name = API_KEY_ENV.get(provider)
    if secret_name is None:
        raise ProviderSmokeError(f"unsupported provider: {provider}")
    api_key = os.environ.get("PROVIDER_API_KEY") or os.environ.get(secret_name, "")
    if not api_key:
        raise ProviderSmokeError(f"{secret_name} is unavailable")
    catalog: list[str] = []
    if discover:
        try:
            catalog = discover_models(provider, api_key)
        except ProviderSmokeError as exc:
            print(json.dumps({"provider": provider, "catalog": "unavailable", "error": str(exc)}))
    candidates = select_candidates(catalog, requested, limit)
    if not candidates:
        raise ProviderSmokeError("no candidate models were selected")
    failures: list[str] = []
    for model in candidates:
        try:
            probe_model(
                provider,
                model,
                api_key,
                builder_shape=builder_shape,
                max_output_tokens=max_output_tokens,
                prompt_bytes=prompt_bytes,
                visual_input=visual_input,
            )
            print(json.dumps({"provider": provider, "model": model, "tool_loop": "pass", "builder_shape": builder_shape, "visual_input": visual_input, "max_output_tokens": max_output_tokens, "prompt_bytes": prompt_bytes}))
            return model
        except (ProviderSmokeError, json.JSONDecodeError) as exc:
            safe_error = str(exc).replace(api_key, "[redacted]")
            failures.append(f"{model}: {safe_error}")
            print(json.dumps({"provider": provider, "model": model, "tool_loop": "fail", "error": safe_error}))
    raise ProviderSmokeError("all candidates failed: " + "; ".join(failures))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="nvidia", choices=sorted(ENDPOINTS))
    parser.add_argument("--models-csv", default="")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--max-candidates", type=int, default=4)
    parser.add_argument("--builder-shape", action="store_true")
    parser.add_argument("--max-output-tokens", type=int, default=128)
    parser.add_argument("--prompt-bytes", type=int, default=0)
    parser.add_argument("--visual-input", action="store_true")
    args = parser.parse_args()
    requested = [item.strip() for item in args.models_csv.split(",") if item.strip()]
    run(
        args.provider,
        requested,
        discover=args.discover,
        limit=args.max_candidates,
        builder_shape=args.builder_shape,
        max_output_tokens=args.max_output_tokens,
        prompt_bytes=args.prompt_bytes,
        visual_input=args.visual_input,
    )


if __name__ == "__main__":
    main()
