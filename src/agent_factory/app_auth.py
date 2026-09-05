"""Short-lived GitHub App installation tokens for review publication."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import jwt


class AppAuthError(RuntimeError):
    pass


_CACHE: dict[str, tuple[str, datetime]] = {}
_SAFETY_MARGIN = timedelta(minutes=5)
_API = "https://api.github.com"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _request(url: str, method: str, token: str) -> dict:
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-factory-app-auth/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise AppAuthError(f"GitHub API {method} failed: {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise AppAuthError(f"GitHub API {method} unreachable: {exc.reason}") from exc
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise AppAuthError("GitHub API returned malformed JSON") from exc


def get_installation_token(
    repo: str,
    *,
    app_id: str | None = None,
    private_key: str | None = None,
) -> str:
    cached = _CACHE.get(repo)
    if cached and _now() + _SAFETY_MARGIN < cached[1]:
        return cached[0]
    if repo.count("/") != 1 or any(not part for part in repo.split("/")):
        raise AppAuthError(f"repo must be owner/name, got {repo!r}")

    app_id = app_id or os.environ.get("AGENT_FACTORY_APP_ID")
    private_key = private_key or os.environ.get("AGENT_FACTORY_APP_PRIVATE_KEY")
    if not app_id:
        raise AppAuthError("AGENT_FACTORY_APP_ID is required")
    if not private_key:
        raise AppAuthError("AGENT_FACTORY_APP_PRIVATE_KEY is required")

    now = int(time.time())
    try:
        app_jwt = jwt.encode(
            {"iat": now - 60, "exp": now + 600, "iss": str(app_id)},
            private_key,
            algorithm="RS256",
        )
    except Exception as exc:  # signing libraries expose several key errors
        raise AppAuthError(f"failed to sign GitHub App JWT: {exc}") from exc

    installation = _request(f"{_API}/repos/{repo}/installation", "GET", app_jwt)
    installation_id = installation.get("id")
    if not isinstance(installation_id, int):
        raise AppAuthError(f"App is not installed for {repo}")
    exchange = _request(
        f"{_API}/app/installations/{installation_id}/access_tokens", "POST", app_jwt
    )
    token, expires_at = exchange.get("token"), exchange.get("expires_at")
    if not isinstance(token, str) or not isinstance(expires_at, str):
        raise AppAuthError("installation-token response is incomplete")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppAuthError("installation-token expiry is invalid") from exc
    _CACHE[repo] = (token, expiry)
    return token
