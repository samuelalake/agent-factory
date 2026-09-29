from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from agent_factory.figma_mcp import (
    FigmaMCPError,
    MCP_RESOURCE,
    invalidate_job_token,
    prepare,
    refresh_access_token,
    write_mcp_config,
)


class FigmaMCPTests(unittest.TestCase):
    @mock.patch("agent_factory.figma_mcp._json_request")
    def test_refresh_uses_refresh_grant_and_resource(self, request_json) -> None:
        request_json.return_value = {"access_token": "job-token"}
        token = refresh_access_token("client", "secret", "refresh")
        self.assertEqual(token, "job-token")
        request = request_json.call_args.args[0]
        body = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(body["grant_type"], ["refresh_token"])
        self.assertEqual(body["refresh_token"], ["refresh"])
        self.assertEqual(body["resource"], [MCP_RESOURCE])
        self.assertTrue(request.headers["Authorization"].startswith("Basic "))

    def test_refresh_fails_without_all_long_lived_credentials(self) -> None:
        with self.assertRaisesRegex(FigmaMCPError, "missing"):
            refresh_access_token("client", "", "refresh")

    @mock.patch("agent_factory.figma_mcp._json_request")
    def test_refresh_fails_closed_if_server_rotates_refresh_token(self, request_json) -> None:
        request_json.return_value = {
            "access_token": "job-token",
            "refresh_token": "unexpected-new-refresh",
        }
        with self.assertRaisesRegex(FigmaMCPError, "rotated"):
            refresh_access_token("client", "secret", "refresh")

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

    @mock.patch("agent_factory.figma_mcp.refresh_access_token", return_value="job-token")
    def test_prepare_writes_only_short_lived_values_to_job_environment(self, refresh) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "figma.json"
            github_env = Path(tmp) / "github-env"
            source = {
                "FIGMA_MCP_CLIENT_ID": "client",
                "FIGMA_MCP_CLIENT_SECRET": "secret",
                "FIGMA_MCP_REFRESH_TOKEN": "refresh",
            }
            with mock.patch.dict(os.environ, source, clear=True):
                prepare(config, github_env)
            env_text = github_env.read_text()
            self.assertIn("FIGMA_MCP_ACCESS_TOKEN=job-token", env_text)
            self.assertIn(f"AGENT_FACTORY_MCP_CONFIG={config}", env_text)
            self.assertNotIn("refresh", env_text)
            self.assertNotIn("secret", env_text)

    @mock.patch("agent_factory.figma_mcp.refresh_access_token")
    def test_invalidate_exchanges_and_discards_replacement(self, refresh) -> None:
        source = {
            "FIGMA_MCP_CLIENT_ID": "client",
            "FIGMA_MCP_CLIENT_SECRET": "secret",
            "FIGMA_MCP_REFRESH_TOKEN": "refresh",
        }
        with mock.patch.dict(os.environ, source, clear=True):
            self.assertIsNone(invalidate_job_token())
        refresh.assert_called_once_with("client", "secret", "refresh")

    @mock.patch("agent_factory.figma_mcp.subprocess.run")
    def test_secret_value_is_sent_on_stdin_not_process_arguments(self, run) -> None:
        from agent_factory.figma_mcp import _save_secret

        _save_secret("owner/repo", "FIGMA_MCP_CLIENT_SECRET", "private-value")
        invocation = run.call_args
        self.assertNotIn("private-value", invocation.args[0])
        self.assertEqual(invocation.kwargs["input"], "private-value")
        self.assertTrue(invocation.kwargs["capture_output"])


if __name__ == "__main__":
    unittest.main()
