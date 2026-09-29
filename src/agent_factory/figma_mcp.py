"""Figma MCP OAuth credentials for hosted Figma Writer runs.

The authorization command is deliberately operator-run: it completes Figma's
browser consent locally and writes the resulting MCP access grant straight to
GitHub Actions secrets. The hosted prepare command exposes that grant only to
the leased Writer phase; Builder and Reviewer jobs never receive it.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any


MCP_RESOURCE = "https://mcp.figma.com/mcp"
REGISTRATION_ENDPOINT = "https://api.figma.com/v1/oauth/mcp/register"
AUTHORIZATION_ENDPOINT = "https://www.figma.com/oauth/mcp"
TOKEN_ENDPOINT = "https://api.figma.com/v1/oauth/token"
EXPECTED_ISSUER = "https://api.figma.com"
MCP_SCOPE = "mcp:connect"
OAUTH_BUNDLE_SECRET_NAME = "FIGMA_MCP_OAUTH_BUNDLE"


class FigmaMCPError(RuntimeError):
    """A safe-to-report Figma OAuth setup or exchange failure."""


def _json_request(
    request: urllib.request.Request,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # OAuth error identifiers such as ``invalid_grant`` are useful for
        # recovery and are safe to report. Never surface the response body or
        # description: either could echo a one-time code or credential.
        error_name = ""
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            candidate = (
                error_payload.get("error")
                if isinstance(error_payload, dict)
                else ""
            )
            if isinstance(candidate, str) and re.fullmatch(
                r"[A-Za-z0-9_.-]{1,64}", candidate
            ):
                error_name = candidate
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        suffix = f" ({error_name})" if error_name else ""
        raise FigmaMCPError(
            f"Figma OAuth request failed with HTTP {exc.code}{suffix}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FigmaMCPError("Figma OAuth request could not reach the server") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FigmaMCPError("Figma OAuth returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise FigmaMCPError("Figma OAuth returned an invalid response shape")
    return payload


def _token_request(
    form: dict[str, str],
    *,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    form = dict(form)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    # Figma documents HTTP Basic client authentication for both authorization
    # code exchange and refresh. Keep the secret out of the request body and
    # pair this with the registration method declared below.
    credentials = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode("ascii")
    headers["Authorization"] = f"Basic {credentials}"
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=urllib.parse.urlencode(form).encode(),
        headers=headers,
        method="POST",
    )
    return _json_request(request)


def _oauth_bundle(value: str) -> dict[str, str]:
    """Validate the single-secret OAuth record without ever reporting its values."""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise FigmaMCPError("Figma MCP OAuth bundle is invalid; re-run authorization") from exc
    if not isinstance(payload, dict):
        raise FigmaMCPError("Figma MCP OAuth bundle is invalid; re-run authorization")
    required = ("client_id", "client_secret", "refresh_token")
    if any(not isinstance(payload.get(key), str) or not payload[key] for key in required):
        raise FigmaMCPError("Figma MCP OAuth bundle is incomplete; re-run authorization")
    return {key: payload[key] for key in required}


def _validate_mcp_access(access_token: str, *, timeout: int = 30) -> None:
    """Prove the refreshed token is accepted by the protected MCP resource."""
    request = urllib.request.Request(
        MCP_RESOURCE,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "agent-factory-figma-writer",
                        "version": "1",
                    },
                },
            }
        ).encode(),
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return
    except urllib.error.HTTPError as exc:
        raise FigmaMCPError(
            f"Figma MCP rejected the refreshed access grant with HTTP {exc.code}; "
            "re-run authorization"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FigmaMCPError("Figma MCP access validation could not reach the server") from exc


def write_mcp_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "figma": {
                        "type": "http",
                        "url": MCP_RESOURCE,
                        "headers": {
                            "Authorization": "Bearer ${FIGMA_MCP_ACCESS_TOKEN}"
                        },
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def prepare(config_path: Path, github_env: Path) -> None:
    raw_bundle = os.environ.get(OAUTH_BUNDLE_SECRET_NAME, "")
    if not raw_bundle:
        raise FigmaMCPError(
            "Figma MCP is enabled but its OAuth bundle is missing; "
            "re-run the local authorization command"
        )
    bundle = _oauth_bundle(raw_bundle)
    payload = _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": bundle["refresh_token"],
            "resource": MCP_RESOURCE,
        },
        client_id=bundle["client_id"],
        client_secret=bundle["client_secret"],
    )
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise FigmaMCPError("Figma OAuth refresh returned no access token")
    _validate_mcp_access(access_token)
    write_mcp_config(config_path)
    # GitHub interprets add-mask commands without displaying their payload. The
    # token is then shared only with the subsequent Builder process.
    print(f"::add-mask::{access_token}")
    with github_env.open("a", encoding="utf-8") as handle:
        handle.write(f"FIGMA_MCP_ACCESS_TOKEN={access_token}\n")
        handle.write(f"AGENT_FACTORY_MCP_CONFIG={config_path}\n")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _authorization_url(
    *, client_id: str, redirect_uri: str, challenge: str, state: str
) -> str:
    return AUTHORIZATION_ENDPOINT + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": MCP_SCOPE,
            "resource": MCP_RESOURCE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    query: dict[str, list[str]] = {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        body = b"Figma authorization received. You can close this tab and return to Terminal."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _register_client(redirect_uri: str) -> tuple[str, str]:
    request = urllib.request.Request(
        REGISTRATION_ENDPOINT,
        data=json.dumps(
            {
                # Figma allows only cataloged MCP clients. The hosted phase runs
                # the Claude Code CLI, so this is the truthful supported identity.
                "client_name": "Claude Code",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_basic",
                "application_type": "native",
                "scope": MCP_SCOPE,
            }
        ).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    payload = _json_request(request)
    client_id = payload.get("client_id")
    client_secret = payload.get("client_secret")
    if not isinstance(client_id, str) or not client_id:
        raise FigmaMCPError("Figma client registration returned no client ID")
    if not isinstance(client_secret, str) or not client_secret:
        raise FigmaMCPError("Figma client registration returned no client secret")
    if payload.get("token_endpoint_auth_method") not in (
        None,
        "client_secret_basic",
    ):
        raise FigmaMCPError("Figma registered an unsupported client authentication method")
    return client_id, client_secret


def _save_secret(repo: str, name: str, value: str) -> None:
    try:
        subprocess.run(
            ["gh", "secret", "set", name, "--repo", repo, "--body", "-"],
            input=value,
            text=True,
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise FigmaMCPError("GitHub CLI is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise FigmaMCPError(f"GitHub could not save secret {name}") from exc


def authorize(repo: str, *, timeout: int = 300) -> None:
    _CallbackHandler.query = {}
    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.timeout = timeout
    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    client_id, client_secret = _register_client(redirect_uri)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    authorization_url = _authorization_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        challenge=challenge,
        state=state,
    )
    print("Opening Figma. Approve the MCP connection in your browser.")
    if not webbrowser.open(authorization_url):
        print(f"Open this authorization URL manually:\n{authorization_url}")
    server.handle_request()
    server.server_close()
    query = _CallbackHandler.query
    if query.get("state", [""])[0] != state:
        raise FigmaMCPError("Figma OAuth callback state did not match")
    issuer = query.get("iss", [""])[0]
    if issuer and issuer != EXPECTED_ISSUER:
        raise FigmaMCPError("Figma OAuth callback issuer did not match")
    if query.get("error"):
        raise FigmaMCPError("Figma authorization was denied or failed")
    code = query.get("code", [""])[0]
    if not code:
        raise FigmaMCPError("Figma authorization timed out or returned no code")
    payload = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": MCP_RESOURCE,
        },
        client_id=client_id,
        client_secret=client_secret,
    )
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise FigmaMCPError("Figma authorization returned no access token")
    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise FigmaMCPError("Figma authorization returned no refresh token")
    _validate_mcp_access(access_token)
    bundle = json.dumps(
        {
            "version": 1,
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        separators=(",", ":"),
    )
    _save_secret(repo, OAUTH_BUNDLE_SECRET_NAME, bundle)
    print(f"Saved {OAUTH_BUNDLE_SECRET_NAME} to {repo} without printing its value.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Figma MCP OAuth for Agent Factory")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--config", type=Path, required=True)
    prepare_parser.add_argument("--github-env", type=Path, required=True)
    authorize_parser = subparsers.add_parser("authorize")
    authorize_parser.add_argument("--repo", required=True)
    authorize_parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.config, args.github_env)
        else:
            authorize(args.repo, timeout=args.timeout)
    except FigmaMCPError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
