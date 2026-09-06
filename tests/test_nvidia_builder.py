from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_factory.nvidia_builder import NvidiaBuilderError, _execute_tool, _inside, _post


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


if __name__ == "__main__":
    unittest.main()
