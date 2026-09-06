from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_factory.cli import default_config
from agent_factory.github_integration import check_state, format_integration, run
from agent_factory.protocol import decode_data


class IntegrationTests(unittest.TestCase):
    def test_required_checks_all_pass(self) -> None:
        state, _ = check_state(
            ("verify", "merge-gate"),
            [
                {"name": "verify", "conclusion": "SUCCESS"},
                {"context": "merge-gate", "state": "SUCCESS"},
            ],
        )
        self.assertEqual(state, "success")

    def test_missing_and_failed_checks_do_not_promote(self) -> None:
        self.assertEqual(check_state(("verify",), [])[0], "pending")
        self.assertEqual(
            check_state(("verify",), [{"name": "verify", "conclusion": "FAILURE"}])[0],
            "failure",
        )

    def test_steward_decision_is_current_head_bound(self) -> None:
        body = format_integration(
            "<!-- integration:test -->", "abc123", "ready", "Passed.", "development"
        )
        data = decode_data(body)
        self.assertIn("## Steward · integration", body)
        self.assertEqual(data["head_sha"], "abc123")
        self.assertEqual(data["next_owner"], "landing")

    @patch("agent_factory.github_integration._upsert_steward_comment")
    @patch("agent_factory.github_integration._set_status")
    @patch("agent_factory.github_integration.recompute_gate")
    @patch("agent_factory.github_integration._gh")
    def test_integration_recomputes_gate_and_lands_immediately(
        self, gh, gate, set_status, upsert
    ) -> None:
        gh.side_effect = [
            json.dumps(
                {
                    "headRefOid": "abc123",
                    "mergeable": "MERGEABLE",
                    "statusCheckRollup": [
                        {"name": "verify", "conclusion": "SUCCESS"},
                        {"context": "merge-gate", "state": "SUCCESS"},
                    ],
                }
            ),
            "",
        ]
        config = default_config("fixture")
        config["gate"]["context"] = "merge-gate"
        config["gate"]["required_checks"] = ["verify"]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.dict(
                os.environ,
                {"GITHUB_TOKEN": "actions", "STEWARD_TOKEN": "steward"},
            ):
                self.assertEqual(
                    run("owner/repo", "7", path, timeout_seconds=1), "ready"
                )

        gate.assert_called_once_with("owner/repo", "7", path)
        set_status.assert_called_once()
        upsert.assert_called_once()
        self.assertEqual(
            gh.call_args_list[-1].args[0],
            ["pr", "merge", "7", "--repo", "owner/repo", "--squash", "--delete-branch"],
        )


if __name__ == "__main__":
    unittest.main()
