from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest import mock

from agent_factory.figma_mcp import (
    ACCESS_SECRET_NAME,
    FigmaMCPError,
    MCP_RESOURCE,
    _json_request,
    _register_client,
    prepare,
    write_mcp_config,
)


class FigmaMCPTests(unittest.TestCase):
    @mock.patch("agent_factory.figma_mcp._json_request")
    def test_registration_uses_supported_claude_code_client(self, request_json) -> None:
        request_json.return_value = {
            "client_id": "client",
            "client_secret": "secret",
            "token_endpoint_auth_method": "client_secret_basic",
        }
        client_id, client_secret = _register_client("http://127.0.0.1:19876/callback")
        self.assertEqual((client_id, client_secret), ("client", "secret"))
        request = request_json.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["client_name"], "Claude Code")
        self.assertEqual(body["token_endpoint_auth_method"], "client_secret_basic")
        self.assertEqual(body["application_type"], "native")

    @mock.patch("agent_factory.figma_mcp.urllib.request.urlopen")
    def test_http_error_reports_only_safe_oauth_identifier(self, urlopen) -> None:
        response_body = json.dumps(
            {
                "error": "invalid_grant",
                "error_description": "authorization code private-code was rejected",
            }
        ).encode()
        urlopen.side_effect = urllib.error.HTTPError(
            "https://api.figma.com/v1/oauth/token",
            400,
            "Bad Request",
            {},
            BytesIO(response_body),
        )
        request = urllib.request.Request("https://api.figma.com/v1/oauth/token")
        with self.assertRaisesRegex(
            FigmaMCPError,
            r"HTTP 400 \(invalid_grant\)",
        ) as raised:
            _json_request(request)
        self.assertNotIn("private-code", str(raised.exception))

    def test_mcp_config_uses_environment_expansion_and_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "figma.json"
            write_mcp_config(path)
            payload = json.loads(path.read_text())
            server = payload["mcpServers"]["figma"]
            self.assertEqual(server["url"], MCP_RESOURCE)
            self.assertEqual(
                server["headers"]["Authorization"],
                "Bearer ${FIGMA_MCP_ACCESS_TOKEN}",
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_prepare_writes_only_leased_values_to_job_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "figma.json"
            github_env = Path(tmp) / "github-env"
            source = {ACCESS_SECRET_NAME: "authorized-access"}
            with mock.patch.dict(os.environ, source, clear=True):
                prepare(config, github_env)
            env_text = github_env.read_text()
            self.assertIn("FIGMA_MCP_ACCESS_TOKEN=authorized-access", env_text)
            self.assertIn(f"AGENT_FACTORY_MCP_CONFIG={config}", env_text)

    def test_prepare_fails_closed_without_authorized_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(FigmaMCPError, "authorization command"),
            ):
                prepare(Path(tmp) / "figma.json", Path(tmp) / "github-env")

    @mock.patch("agent_factory.figma_mcp.subprocess.run")
    def test_secret_value_is_sent_on_stdin_not_process_arguments(self, run) -> None:
        from agent_factory.figma_mcp import _save_secret

        _save_secret("owner/repo", ACCESS_SECRET_NAME, "private-value")
        invocation = run.call_args
        self.assertNotIn("private-value", invocation.args[0])
        self.assertEqual(invocation.kwargs["input"], "private-value")
        self.assertTrue(invocation.kwargs["capture_output"])


if __name__ == "__main__":
    unittest.main()
