"""Provider-neutral text generation for normalized agent contracts."""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class ModelError(RuntimeError):
    pass


def _split_data_url(value: str) -> tuple[str, str]:
    """Return a validated base64 media type and payload for provider adapters."""
    prefix, separator, data = value.partition(",")
    if not separator or not prefix.startswith("data:image/") or ";base64" not in prefix:
        raise ModelError("visual evidence must be a base64 image data URL")
    media_type = prefix[5:].split(";", 1)[0]
    if media_type not in {"image/png", "image/jpeg", "image/webp"} or not data:
        raise ModelError("visual evidence uses an unsupported image type")
    return media_type, data


def _post(url: str, payload: dict, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Provider bodies are arbitrary and may reflect prompts or secrets.
            # Discard them and expose only bounded, allowlisted rate metadata.
            exc.read(8_192)
            metadata = ", ".join(
                f"{name}={value}"
                for name, value in (
                    ("retry_after", exc.headers.get("Retry-After") if exc.headers else None),
                    (
                        "remaining",
                        exc.headers.get("x-ratelimit-remaining") if exc.headers else None,
                    ),
                    (
                        "reset",
                        exc.headers.get("x-ratelimit-reset") if exc.headers else None,
                    ),
                )
                if value is not None
            )
            suffix = f" ({metadata})" if metadata else ""
            transient = exc.code in {429, 500, 502, 503, 504}
            if transient and attempt < 2:
                retry_after = str(exc.headers.get("Retry-After") or "").strip()
                delay = int(retry_after) if retry_after.isdigit() else 5 * (attempt + 1)
                time.sleep(max(1, min(delay, 30)))
                continue
            raise ModelError(f"model HTTP {exc.code}{suffix}") from exc
        except urllib.error.URLError as exc:
            raise ModelError(f"model endpoint unreachable: {exc.reason}") from exc
        except (ConnectionError, TimeoutError, http.client.HTTPException) as exc:
            raise ModelError(f"model transport failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ModelError("model returned invalid JSON") from exc
    raise ModelError("model request exhausted retries")


def complete(
    provider: str,
    model: str,
    system: str,
    user: str,
    api_key: str,
    *,
    image_urls: tuple[str, ...] = (),
) -> str:
    """Return model text while keeping the role contract provider-independent."""
    if not api_key:
        raise ModelError("MODEL_API_KEY is required")
    if provider == "anthropic":
        content: list[dict] = [{"type": "text", "text": user}]
        content.extend(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }
            for media_type, data in map(_split_data_url, image_urls)
        )
        response = _post(
            "https://api.anthropic.com/v1/messages",
            {
                "model": model,
                "max_tokens": 8000,
                "system": system,
                "messages": [{"role": "user", "content": content}],
            },
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        return "".join(
            str(block.get("text") or "")
            for block in response.get("content") or []
            if block.get("type") == "text"
        )
    if provider == "gemini":
        parts: list[dict] = [{"text": user}]
        parts.extend(
            {"inline_data": {"mime_type": media_type, "data": data}}
            for media_type, data in map(_split_data_url, image_urls)
        )
        encoded_model = urllib.parse.quote(model, safe="-._")
        response = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:generateContent",
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts}],
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
    if provider in {"minimax", "openrouter", "nvidia"}:
        endpoints = {
            "minimax": "https://api.minimax.io/v1/chat/completions",
            "openrouter": "https://openrouter.ai/api/v1/chat/completions",
            "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
        }
        user_content: str | list[dict] = user
        if image_urls:
            # OpenAI-compatible providers accept the original data URL directly.
            # Validate it first so malformed or non-image input never crosses the boundary.
            for image_url in image_urls:
                _split_data_url(image_url)
            user_content = [
                {"type": "text", "text": user},
                *(
                    {"type": "image_url", "image_url": {"url": image_url}}
                    for image_url in image_urls
                ),
            ]
        payload = {
            "model": model,
            "max_tokens": 8000,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        }
        if provider == "minimax":
            # M2.x always reasons. Keep that reasoning out of content so the
            # shared role protocol receives only the requested JSON object.
            payload["reasoning_split"] = True
        else:
            payload["response_format"] = {"type": "json_object"}
        response = _post(
            endpoints[provider],
            payload,
            {"Authorization": f"Bearer {api_key}"},
        )
        choices = response.get("choices") or []
        return str(((choices[0].get("message") or {}).get("content") or "")) if choices else ""
    raise ModelError(f"unsupported model provider: {provider}")
