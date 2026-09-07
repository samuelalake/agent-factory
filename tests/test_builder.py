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
    _builder_summary,
    _quota_delay,
    _review_feedback,
    _preserve_workflow_control_plane,
    _run_gemini,
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
            "MINIMAX_API_KEY": "minimax",
            "OPENROUTER_API_KEY": "openrouter",
            "GH_TOKEN": "github",
            "AGENT_FACTORY_BUILDER_APP_PRIVATE_KEY": "private",
        }
        with mock.patch.dict("os.environ", source, clear=True):
            safe = _safe_agent_env()
        self.assertEqual(safe["GEMINI_API_KEY"], "gemini")
        self.assertEqual(safe["GEMINI_CLI_TRUST_WORKSPACE"], "true")
        self.assertNotIn("NVIDIA_API_KEY", safe)
        self.assertNotIn("MINIMAX_API_KEY", safe)
        self.assertNotIn("OPENROUTER_API_KEY", safe)
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

    def test_quota_delay_is_bounded(self) -> None:
        self.assertEqual(_quota_delay("429 Please retry in 50.9s."), 52)
        self.assertEqual(_quota_delay("quota exceeded"), 60)
        self.assertIsNone(_quota_delay("permission denied"))

    def test_gemini_resumes_saved_session_after_rate_limit(self) -> None:
        success = json.dumps({"type": "result", "status": "success"})
        with (
            mock.patch(
                "agent_factory.github_builder._run",
                side_effect=[RuntimeError("429 Please retry in 5s."), success],
            ) as run,
            mock.patch("agent_factory.github_builder.time.sleep") as sleep,
            mock.patch("agent_factory.github_builder.time.monotonic", side_effect=[0, 0, 7]),
        ):
            output = _run_gemini("task", root=Path("."), model="gemini", timeout_seconds=100)
        self.assertEqual(output, success)
        sleep.assert_called_once_with(7)
        self.assertIn("--resume", run.call_args_list[1].args[0])

    def test_builder_prompt_protects_workflow_control_plane(self) -> None:
        config = parse_config(default_config("demo"))
        issue = {"number": 83, "title": "Build the thing", "body": "Acceptance criteria here."}
        with tempfile.TemporaryDirectory() as tmp:
            prompt = build_prompt(config, issue, Path(tmp))
        self.assertIn("Do not edit `.github/workflows/**`", prompt)

    def test_revision_prompt_includes_current_reviewer_findings(self) -> None:
        config = parse_config(default_config("demo"))
        issue = {"number": 83, "title": "Build the thing", "body": "Acceptance criteria here."}
        with tempfile.TemporaryDirectory() as tmp:
            prompt = build_prompt(config, issue, Path(tmp), "[P1] Deliver the promised output.")
        self.assertIn("Current-head Reviewer feedback", prompt)
        self.assertIn("[P1] Deliver the promised output.", prompt)
        self.assertIn("Resolve every finding", prompt)

    def test_current_head_review_feedback_is_selected(self) -> None:
        reviews = [
            {"commit_id": "old", "state": "CHANGES_REQUESTED", "body": "<!-- reviewer:test --> old"},
            {
                "commit_id": "head",
                "state": "CHANGES_REQUESTED",
                "body": "<!-- reviewer:test -->\n[P1] Fix it.\n<!-- agent-factory:data abc -->",
            },
        ]
        with mock.patch("agent_factory.github_builder._gh", return_value=json.dumps(reviews)):
            feedback = _review_feedback(
                "owner/repo", 7, "head", "<!-- reviewer:test -->", root=Path(".")
            )
        self.assertIn("[P1] Fix it.", feedback)
        self.assertNotIn("agent-factory:data", feedback)
        self.assertNotIn("old", feedback)

    def test_builder_summary_drops_model_reasoning(self) -> None:
        response = "<think>private chain of thought</think>\nLet me inspect one more thing:"
        summary = _builder_summary(response, "83")
        self.assertNotIn("chain of thought", summary)
        self.assertNotIn("Let me", summary)
        self.assertIn("issue #83", summary)

    def test_workflow_control_plane_is_restored_without_discarding_product_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import subprocess

            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            workflow = root / ".github/workflows/verify.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("safe: true\n", encoding="utf-8")
            product = root / "Product.swift"
            product.write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)

            workflow.write_text("unsafe: true\n", encoding="utf-8")
            product.write_text("implemented\n", encoding="utf-8")
            new_workflow = root / ".github/workflows/new.yml"
            new_workflow.write_text("unsafe: true\n", encoding="utf-8")

            preserved = _preserve_workflow_control_plane(root)

            self.assertEqual(
                preserved,
                (".github/workflows/new.yml", ".github/workflows/verify.yml"),
            )
            self.assertEqual(workflow.read_text(encoding="utf-8"), "safe: true\n")
            self.assertFalse(new_workflow.exists())
            self.assertEqual(product.read_text(encoding="utf-8"), "implemented\n")


if __name__ == "__main__":
    unittest.main()
