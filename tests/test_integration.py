from __future__ import annotations

import unittest

from agent_factory.github_integration import check_state, format_integration
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


if __name__ == "__main__":
    unittest.main()
