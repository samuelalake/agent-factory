from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import subprocess
from unittest import mock

from agent_factory.nvidia_builder import (
    NvidiaBuilderError,
    _execute_tool,
    _inside,
    _post,
    run_openai_builder,
)


class NvidiaBuilderTests(unittest.TestCase):
    def test_paths_cannot_escape_repository_or_enter_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(NvidiaBuilderError, "escaped"):
                _inside(root, "../secret")
            with self.assertRaisesRegex(NvidiaBuilderError, "git"):
                _inside(root, ".git/config")

    def test_write_and_exact_replace_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _execute_tool(root, "write_file", {"path": "src/demo.txt", "content": "before"})
            result = _execute_tool(
                root,
                "replace_text",
                {"path": "src/demo.txt", "old": "before", "new": "after"},
            )
            self.assertEqual(result["replacements"], 1)
            self.assertEqual((root / "src/demo.txt").read_text(), "after")

    def test_command_policy_denies_publication_and_secret_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for command in ("git push origin main", "gh pr create", "printenv", "sudo true", "rm -rf out"):
                with self.subTest(command=command), self.assertRaisesRegex(NvidiaBuilderError, "rejected"):
                    _execute_tool(root, "run_command", {"command": command})

    def test_post_retries_rate_limit_with_server_delay(self) -> None:
        limited = __import__("urllib.error").error.HTTPError(
            "https://example.test", 429, "limited", {"Retry-After": "2"}, None
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with (
            mock.patch("agent_factory.nvidia_builder.urllib.request.urlopen", side_effect=[limited, response]),
            mock.patch("agent_factory.nvidia_builder.json.load", return_value={"choices": []}),
            mock.patch("agent_factory.nvidia_builder.time.sleep") as sleep,
        ):
            self.assertEqual(_post("model", [], "key", 30), {"choices": []})
        sleep.assert_called_once_with(2)

    def test_post_retries_transient_service_capacity(self) -> None:
        unavailable = __import__("urllib.error").error.HTTPError(
            "https://example.test", 503, "unavailable", {}, None
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with (
            mock.patch(
                "agent_factory.nvidia_builder.urllib.request.urlopen",
                side_effect=[unavailable, response],
            ),
            mock.patch("agent_factory.nvidia_builder.json.load", return_value={"choices": []}),
            mock.patch("agent_factory.nvidia_builder.time.sleep") as sleep,
        ):
            self.assertEqual(_post("model", [], "key", 30), {"choices": []})
        sleep.assert_called_once_with(30)

    def test_minimax_uses_its_openai_compatible_endpoint(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with (
            mock.patch("agent_factory.nvidia_builder.urllib.request.urlopen", return_value=response) as open_url,
            mock.patch("agent_factory.nvidia_builder.json.load", return_value={"choices": []}),
        ):
            _post("MiniMax-M2.7", [], "key", 30, provider="minimax")
        self.assertEqual(
            open_url.call_args.args[0].full_url,
            "https://api.minimax.io/v1/chat/completions",
        )

    def test_usage_stops_the_builder_at_configured_cost_limit(self) -> None:
        response = {
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0},
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
        }
        with mock.patch("agent_factory.nvidia_builder._post", return_value=response):
            with self.assertRaisesRegex(NvidiaBuilderError, "estimated cost limit"):
                run_openai_builder(
                    "task",
                    Path("."),
                    provider="minimax",
                    model="MiniMax-M2.7",
                    api_key="key",
                    max_requests=1,
                    timeout_seconds=60,
                    max_cost_usd=0.25,
                    input_cost_per_million=0.3,
                    output_cost_per_million=1.2,
                )

    def test_priced_provider_must_report_usage(self) -> None:
        response = {"choices": [{"message": {"role": "assistant", "content": "done"}}]}
        with mock.patch("agent_factory.nvidia_builder._post", return_value=response):
            with self.assertRaisesRegex(NvidiaBuilderError, "omitted token usage"):
                run_openai_builder(
                    "task",
                    Path("."),
                    provider="minimax",
                    model="MiniMax-M2.7",
                    api_key="key",
                    max_requests=1,
                    timeout_seconds=60,
                    max_cost_usd=3,
                    input_cost_per_million=0.3,
                    output_cost_per_million=1.2,
                )

    def test_request_boundary_publishes_an_existing_candidate(self) -> None:
        response = {
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path":"candidate.txt","content":"ready"}',
                                },
                            }
                        ],
                    }
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            with mock.patch("agent_factory.nvidia_builder._post", return_value=response):
                summary, tool_count, cost = run_openai_builder(
                    "task",
                    root,
                    provider="minimax",
                    model="MiniMax-M2.7",
                    api_key="key",
                    max_requests=1,
                    timeout_seconds=60,
                    max_cost_usd=3,
                    input_cost_per_million=0.3,
                    output_cost_per_million=1.2,
                )
        self.assertIn("reviewable repository candidate", summary)
        self.assertEqual(tool_count, 1)
        self.assertGreater(cost, 0)


if __name__ == "__main__":
    unittest.main()
