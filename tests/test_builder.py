from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_factory.cli import default_config
from agent_factory.config import parse_config
from agent_factory.github_builder import (
    BuilderBlocked,
    _safe_agent_env,
    build_prompt,
    format_issue_status,
    parse_gemini_stream,
)
from agent_factory.protocol import decode_data


class BuilderTests(unittest.TestCase):
    def test_prompt_briefs_agent_without_hardcoding_consumer(self) -> None:
        config = parse_config(default_config("demo"))
        issue = {"number": 83, "title": "Build the thing", "body": "Acceptance criteria here."}
        with tempfile.TemporaryDirectory() as tmp:
            prompt = build_prompt(config, issue, Path(tmp))
        self.assertIn("GitHub issue #83", prompt)
        self.assertIn("Acceptance criteria here.", prompt)
        self.assertIn("Discover and follow repository instructions", prompt)
        self.assertNotIn("Swami", prompt)

    def test_agent_environment_excludes_role_credentials(self) -> None:
        source = {
            "PATH": "/bin",
            "HOME": "/tmp/home",
            "GEMINI_API_KEY": "gemini",
            "NVIDIA_API_KEY": "nvidia",
            "GH_TOKEN": "github",
            "AGENT_FACTORY_BUILDER_APP_PRIVATE_KEY": "private",
        }
        with mock.patch.dict("os.environ", source, clear=True):
            safe = _safe_agent_env()
        self.assertEqual(safe["GEMINI_API_KEY"], "gemini")
        self.assertEqual(safe["GEMINI_CLI_TRUST_WORKSPACE"], "true")
        self.assertNotIn("NVIDIA_API_KEY", safe)
        self.assertNotIn("GH_TOKEN", safe)
        self.assertFalse(any("PRIVATE_KEY" in key for key in safe))

    def test_delivery_status_has_machine_contract(self) -> None:
        body = format_issue_status(
            "<!-- builder:test -->", "83", "delivered", "Done.", "https://example.test/pr/1"
        )
        data = decode_data(body)
        self.assertIn("## Builder", body)
        self.assertEqual(data["state"], "delivered")
        self.assertEqual(data["pull_request"], "https://example.test/pr/1")

    def test_stream_requires_and_counts_repository_tools(self) -> None:
        output = "\n".join(
            [
                json.dumps({"type": "tool_use", "tool_name": "read_file"}),
                json.dumps({"type": "tool_result", "status": "success"}),
                json.dumps({"type": "message", "role": "assistant", "content": "Implemented."}),
                json.dumps({"type": "result", "status": "success"}),
            ]
        )
        self.assertEqual(parse_gemini_stream(output), ("Implemented.", 1))
        with self.assertRaisesRegex(BuilderBlocked, "without using repository tools"):
            parse_gemini_stream(json.dumps({"type": "result", "status": "success"}))


if __name__ == "__main__":
    unittest.main()
