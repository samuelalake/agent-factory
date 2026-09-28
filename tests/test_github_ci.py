from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_factory.github_ci import collect_ci_failures, diagnostic_excerpt, _read
from agent_factory.github_builder import build_prompt, _safe_agent_env, _claude_agent_env
from agent_factory.cli import default_config, install
from agent_factory.config import parse_config

HEAD = "a" * 40


def run(run_id=10, workflow=1, **changes):
    return {"id": run_id, "workflow_id": workflow, "head_sha": HEAD,
            "head_repository": {"full_name": "owner/repo"}, "event": "pull_request",
            "status": "completed", "conclusion": "failure", **changes}


class CIDiagnosticTests(unittest.TestCase):
    @mock.patch("agent_factory.github_ci._read")
    def test_current_head_compiler_failure_reaches_builder_prompt(self, read):
        read.side_effect = [json.dumps({"workflow_runs": [run()]}),
                            "setup done\ne: file:///repo/icons.kt:32: ExperimentalTextApi opt-in required\nBUILD FAILED"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = collect_ci_failures("owner/repo", HEAD, root=root, token="test-read-token")
            prompt = build_prompt(parse_config(default_config("demo")),
                                  {"number": 30, "title": "Consent", "body": "Approved contract"},
                                  root, ci_diagnostics=report)
        self.assertIn("ExperimentalTextApi opt-in required", prompt)
        self.assertIn("https://github.com/owner/repo/actions/runs/10", prompt)
        self.assertIn(HEAD, prompt)
        self.assertIn("never instructions", prompt)
        self.assertNotIn("test-read-token", prompt)
        self.assertEqual(read.call_args_list[1].args[0],
                         ["run", "view", "10", "--repo", "owner/repo", "--log-failed"])

    @mock.patch("agent_factory.github_ci._read")
    def test_stale_foreign_and_superseded_failures_are_excluded(self, read):
        read.return_value = json.dumps({"workflow_runs": [
            run(1), run(2, conclusion="success"),
            run(3, 2, head_sha="b" * 40),
            run(4, 3, head_repository={"full_name": "fork/repo"}),
            run(5, 4, event="pull_request_target"),
            run(6, 5), run(7, 5, status="in_progress", conclusion=None),
        ]})
        result = collect_ci_failures("owner/repo", HEAD, root=Path("."), token="test")
        self.assertIn("does not establish that CI passed", result)
        self.assertEqual(read.call_count, 1)

    @mock.patch("agent_factory.github_ci._read")
    def test_failed_run_reads_are_bounded(self, read):
        read.side_effect = [json.dumps({"workflow_runs": [run(i, i) for i in range(1, 8)]}),
                            "error one", "error two", "error three"]
        report = json.loads(collect_ci_failures("owner/repo", HEAD, root=Path("."), token="test"))
        self.assertEqual(len(report["runs"]), 3)
        self.assertEqual(report["omitted_failed_runs"], 4)
        self.assertEqual(read.call_count, 4)

    @mock.patch("agent_factory.github_ci._read", side_effect=RuntimeError("private arbitrary output"))
    def test_unavailable_metadata_is_explicit_without_raw_error(self, read):
        report = collect_ci_failures("owner/repo", HEAD, root=Path("."), token="test")
        self.assertIn("unavailable", report)
        self.assertNotIn("private arbitrary output", report)

    @mock.patch("agent_factory.github_ci._read")
    def test_log_timeout_preserves_run_link_without_raw_error(self, read):
        read.side_effect = [json.dumps({"workflow_runs": [run()]}), subprocess.TimeoutExpired("gh", 60)]
        report = collect_ci_failures("owner/repo", HEAD, root=Path("."), token="test")
        self.assertIn("actions/runs/10", report)
        self.assertIn("log unavailable", report)

    @mock.patch("agent_factory.github_ci._read")
    def test_missing_read_token_does_not_borrow_builder_credentials(self, read):
        with mock.patch.dict("os.environ", {"GH_TOKEN": "builder-token"}):
            report = collect_ci_failures("owner/repo", HEAD, root=Path("."), token="")
        self.assertIn("Actions read", report)
        read.assert_not_called()

    def test_excerpt_omits_credentials_and_preserves_error_context(self):
        report = diagnostic_excerpt("\n".join([
            "TOKEN: sensitive-value", "Authorization: Bearer sensitive-value",
            "e: file:///repo/icons.kt:32: opt-in required", "at compileDebugKotlin",
            "github_pat_examplevalue", "API_KEY=secret-value", "Build failed",
        ]))
        self.assertIn("opt-in required", report)
        self.assertIn("compileDebugKotlin", report)
        for value in ("sensitive-value", "secret-value", "github_pat_examplevalue"):
            self.assertNotIn(value, report)
        self.assertLess(len(diagnostic_excerpt("error " + "x" * 100_000)), 12_100)

    def test_ci_read_credential_is_excluded_from_agent_environments(self):
        with mock.patch.dict("os.environ", {"AGENT_FACTORY_CI_READ_TOKEN": "ci-token"}):
            self.assertNotIn("AGENT_FACTORY_CI_READ_TOKEN", _safe_agent_env())
            self.assertNotIn("AGENT_FACTORY_CI_READ_TOKEN", _claude_agent_env())

    def test_installed_caller_grants_actions_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            install(root, factory_ref="test", force=False)
            self.assertIn("  actions: read", (root / ".github/workflows/agent-builder.yml").read_text())

    @mock.patch("agent_factory.github_ci.subprocess.run")
    def test_reader_caps_log_output_and_hides_credentials_from_args(self, execute):
        def write_output(args, **kwargs):
            kwargs["stdout"].write(b"x" * 2_000_050)
            self.assertEqual(kwargs["env"]["GH_TOKEN"], "test-token")
            self.assertNotIn("test-token", str(args))
            self.assertNotIn("MODEL_API_KEY", kwargs["env"])
            return subprocess.CompletedProcess(args, 0)
        execute.side_effect = write_output
        with mock.patch.dict("os.environ", {"MODEL_API_KEY": "model-secret"}):
            report = _read(["run", "view", "10", "--log-failed"], root=Path("."), token="test-token")
        self.assertTrue(report.startswith("[log tail"))
        self.assertLess(len(report), 2_000_100)
