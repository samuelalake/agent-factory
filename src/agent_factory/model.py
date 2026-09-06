"""Provider-neutral text generation for normalized agent contracts."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class ModelError(RuntimeError):
    pass


def _post(url: str, payload: dict, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise ModelError(f"model HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ModelError(f"model endpoint unreachable: {exc.reason}") from exc


def complete(provider: str, model: str, system: str, user: str, api_key: str) -> str:
    """Return model text while keeping the role contract provider-independent."""
    if not api_key:
        raise ModelError("MODEL_API_KEY is required")
    if provider == "anthropic":
        response = _post(
            "https://api.anthropic.com/v1/messages",
            {
                "model": model,
                "max_tokens": 8000,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        return "".join(
            str(block.get("text") or "")
            for block in response.get("content") or []
            if block.get("type") == "text"
        )
    if provider == "gemini":
        encoded_model = urllib.parse.quote(model, safe="-._")
        response = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:generateContent",
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "maxOutputTokens": 8000,
                    "responseMimeType": "application/json",
                },
            },
            {"x-goog-api-key": api_key},
        )
        candidates = response.get("candidates") or []
        parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
        return "".join(str(part.get("text") or "") for part in parts)
    if provider == "openrouter":
        response = _post(
            "https://openrouter.ai/api/v1/chat/completions",
            {
                "model": model,
                "max_tokens": 8000,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            },
            {"Authorization": f"Bearer {api_key}"},
        )
        choices = response.get("choices") or []
        return str(((choices[0].get("message") or {}).get("content") or "")) if choices else ""
    if provider == "nvidia":
        response = _post(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            {
                "model": model,
                "max_tokens": 8000,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            },
            {"Authorization": f"Bearer {api_key}"},
        )
        choices = response.get("choices") or []
        return str(((choices[0].get("message") or {}).get("content") or "")) if choices else ""
    raise ModelError(f"unsupported model provider: {provider}")
