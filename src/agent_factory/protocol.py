"""Stable review artifact protocol shared by publisher and gate."""
from __future__ import annotations

import base64
import json
import re
from typing import Any

DATA_PREFIX = "<!-- agent-factory:data "
DATA_RE = re.compile(r"<!-- agent-factory:data ([A-Za-z0-9_-]+) -->")


def encode_data(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{DATA_PREFIX}{token} -->"


def decode_data(body: str) -> dict[str, Any] | None:
    match = DATA_RE.search(body)
    if match is None:
        return None
    token = match.group(1)
    token += "=" * (-len(token) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(token).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def extract_json_reply(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("model reply must be a JSON object")
    return value
