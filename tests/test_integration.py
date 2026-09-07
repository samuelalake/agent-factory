from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_factory.cli import default_config
from agent_factory.config import parse_config
from agent_factory.github_integration import (
    check_state,
    close_delivered_issues,
    ensure_followup_issue,
    format_integration,
    integration_environment,
    linked_issue_numbers,
    queue_followup_for_steward,
    report_landing_permission_failure,
    review_followup_issue_number,
    route_failure,
    run,
)
from agent_factory.protocol import decode_data, encode_data


class IntegrationTests(unittest.TestCase):
    def test_environment_follows_actual_integration_target(self) -> None:
        config = parse_config(default_config("fixture"))
        self.assertEqual(integration_environment(config, "development"), "development")
        self.assertEqual(integration_environment(config, "main"), "production")
        self.assertEqual(integration_environment(config, "release-candidate"), "release-candidate")

    def test_linked_issue_numbers_are_deduplicated(self) -> None:
        self.assertEqual(linked_issue_numbers("Closes #83 and fixes #83; resolves #91"), ("83", "91"))

    @patch("agent_factory.github_integration._gh")
    def test_failed_revision_routes_linked_issue_back_to_builder(self, gh) -> None:
        config = parse_config(default_config("fixture"))
        detail = route_failure(
            "owner/repo",
            {
                "headRefOid": "head",
                "body": "Closes #83",
                "commits": [{"messageHeadline": "feat: implement issue #83"}],
                "reviews": [],
            },
            config,
            "steward",
        )
        self.assertIn("revision 2 of 3", detail)
        gh.assert_called_once_with(
            ["issue", "edit", "83", "--repo", "owner/repo", "--add-label", "agent:retry"],
            token="steward",
        )

    @patch("agent_factory.github_integration._gh")
    def test_revision_limit_routes_to_steward_without_another_retry(self, gh) -> None:
        config = parse_config(default_config("fixture"))
        detail = route_failure(
            "owner/repo",
            {
                "headRefOid": "head",
                "body": "Closes #83",
                "commits": [
                    {"messageHeadline": "feat: implement issue #83"},
                    {"messageHeadline": "feat: implement issue #83"},
                    {"messageHeadline": "feat: implement issue #83"},
                    {"messageHeadline": "Merge current main"},
                ],
                "reviews": [],
            },
            config,
            "steward",
        )
        self.assertIn("configured limit of 3", detail)
        self.assertEqual(gh.call_args.args[0][-1], "agent:steward")

    @patch("agent_factory.github_integration._gh")
    def test_reviewer_provider_failure_routes_to_steward_not_builder(self, gh) -> None:
        config = parse_config(default_config("fixture"))
        review_body = "\n".join([
            config.review.marker,
            config.review.failure_marker,
            encode_data({"version": 1, "head_sha": "head", "verdict": "request_changes"}),
        ])
        detail = route_failure(
            "owner/repo",
            {
                "headRefOid": "head",
                "body": "Closes #83",
                "commits": [{"messageHeadline": "feat: implement issue #83"}],
                "reviews": [{"body": review_body}],
            },
            config,
            "steward",
        )
        self.assertIn("Reviewer providers", detail)
        self.assertEqual(gh.call_args.args[0][-1], "agent:steward")

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

    def test_review_followup_link_is_recovered_for_later_integration_run(self) -> None:
        self.assertEqual(
            review_followup_issue_number(
                "body\n\n<!-- agent-factory:review-followup-link -->\nReviewer follow-up: #42\n"
            ),
            42,
        )
        self.assertIsNone(review_followup_issue_number("body without a follow-up"))

    @patch("agent_factory.github_integration._gh")
    def test_followup_is_queued_for_steward_after_landing(self, gh) -> None:
        queue_followup_for_steward("owner/repo", 42, "steward")
        gh.assert_called_once_with(
            ["issue", "edit", "42", "--repo", "owner/repo", "--add-label", "agent:steward"],
            token="steward",
        )

    @patch("agent_factory.github_integration._upsert_steward_comment")
    @patch("agent_factory.github_integration._set_status")
    @patch("agent_factory.github_integration._gh")
    def test_permission_preflight_replaces_stale_ready_comment_as_steward(
        self, gh, set_status, upsert
    ) -> None:
        gh.return_value = json.dumps({"headRefOid": "newhead"})
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(default_config("fixture")), encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_TOKEN": "actions", "STEWARD_TOKEN": "steward"}):
                report_landing_permission_failure("owner/repo", "7", path)

        set_status.assert_called_once()
        self.assertEqual(set_status.call_args.args[3], "error")
        self.assertIn("**Failed → Steward**", upsert.call_args.args[3])
        self.assertIn("Contents: write", upsert.call_args.args[3])

    @patch("agent_factory.github_integration._gh")
    def test_steward_creates_one_traceable_followup_and_links_pr(self, gh) -> None:
        review_body = "\n".join([
            "<!-- reviewer:agent-factory -->",
            encode_data({
                "version": 1,
                "head_sha": "abc123",
                "verdict": "approve",
                "findings": [{
                    "severity": "P2",
                    "key": "src/a.py:9",
                    "title": "Cover the edge case",
                    "reasoning": "The fallback is untested.",
                    "suggestion": "Add a regression test.",
                }],
            }),
        ])
        gh.side_effect = [
            json.dumps([[{"state": "APPROVED", "commit_id": "abc123", "body": review_body}]]),
            "[]",
            json.dumps({"number": 42}),
            "",
        ]
        number = ensure_followup_issue(
            "owner/repo",
            "7",
            "abc123",
            "Closes #6",
            "<!-- reviewer:agent-factory -->",
            "actions",
            "steward",
        )
        self.assertEqual(number, 42)
        create_payload = json.loads(gh.call_args_list[2].kwargs["stdin"])
        self.assertIn("<!-- agent-factory:review-followup pr=7 -->", create_payload["body"])
        self.assertIn("- [ ] **[P2] `src/a.py:9`", create_payload["body"])
        self.assertIn("Why: The fallback is untested.", create_payload["body"])
        self.assertIn("Suggested direction: Add a regression test.", create_payload["body"])
        link_payload = json.loads(gh.call_args_list[3].kwargs["stdin"])
        self.assertIn("Reviewer follow-up: #42", link_payload["body"])

    @patch("agent_factory.github_integration._gh")
    def test_steward_updates_existing_followup_instead_of_duplicating(self, gh) -> None:
        review_body = "\n".join([
            "<!-- reviewer:agent-factory -->",
            encode_data({
                "version": 1,
                "head_sha": "newhead",
                "verdict": "approve",
                "findings": [{"severity": "P3", "key": "review-wide", "title": "Clarify docs"}],
            }),
        ])
        gh.side_effect = [
            json.dumps([{"state": "APPROVED", "commit_id": "newhead", "body": review_body}]),
            json.dumps([{
                "number": 42,
                "body": "<!-- agent-factory:review-followup pr=7 -->\nold",
            }]),
            "",
            "",
        ]
        number = ensure_followup_issue(
            "owner/repo",
            "7",
            "newhead",
            "body\n\n<!-- agent-factory:review-followup-link -->\nReviewer follow-up: #41",
            "<!-- reviewer:agent-factory -->",
            "actions",
            "steward",
        )
        self.assertEqual(number, 42)
        self.assertEqual(
            gh.call_args_list[2].args[0][:3],
            ["api", "repos/owner/repo/issues/42", "-X"],
        )
        link_payload = json.loads(gh.call_args_list[3].kwargs["stdin"])
        self.assertIn("Reviewer follow-up: #42", link_payload["body"])
        self.assertNotIn("Reviewer follow-up: #41", link_payload["body"])

    @patch("agent_factory.github_integration._gh")
    def test_landing_closes_source_issue_and_clears_agent_labels(self, gh) -> None:
        config = parse_config(default_config("fixture"))
        gh.side_effect = [
            json.dumps({
                "labels": [
                    {"name": "P1"},
                    {"name": "agent:builder"},
                    {"name": "agent:retry"},
                    {"name": "agent:steward"},
                ]
            }),
            "",
        ]
        close_delivered_issues("owner/repo", "Closes #83", config, "steward")
        payload = json.loads(gh.call_args_list[1].kwargs["stdin"])
        self.assertEqual(payload, {
            "state": "closed",
            "state_reason": "completed",
            "labels": ["P1"],
        })

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
                    "body": "<!-- agent-factory:review-followup-link -->\nReviewer follow-up: #42",
                    "statusCheckRollup": [
                        {"name": "verify", "conclusion": "SUCCESS"},
                        {"context": "merge-gate", "state": "SUCCESS"},
                    ],
                }
            ),
            "",
            json.dumps({"labels": []}),
            "",
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
            gh.call_args_list[1].args[0],
            ["pr", "merge", "7", "--repo", "owner/repo", "--squash", "--delete-branch"],
        )
        self.assertEqual(gh.call_args_list[1].kwargs["token"], "steward")
        self.assertEqual(
            gh.call_args_list[-1].args[0],
            ["issue", "edit", "42", "--repo", "owner/repo", "--add-label", "agent:steward"],
        )

    @patch("agent_factory.github_integration._upsert_steward_comment")
    @patch("agent_factory.github_integration._set_status")
    @patch("agent_factory.github_integration.recompute_gate")
    @patch("agent_factory.github_integration._gh")
    def test_merge_permission_failure_is_visible_and_not_marked_success(
        self, gh, gate, set_status, upsert
    ) -> None:
        gh.side_effect = [
            json.dumps({
                "headRefOid": "abc123",
                "mergeable": "MERGEABLE",
                "body": "",
                "statusCheckRollup": [
                    {"name": "verify", "conclusion": "SUCCESS"},
                    {"context": "merge-gate", "state": "SUCCESS"},
                ],
            }),
            RuntimeError("Resource not accessible by integration"),
        ]
        config = default_config("fixture")
        config["gate"]["context"] = "merge-gate"
        config["gate"]["required_checks"] = ["verify"]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_TOKEN": "actions", "STEWARD_TOKEN": "steward"}):
                with self.assertRaisesRegex(RuntimeError, "Contents: write"):
                    run("owner/repo", "7", path, timeout_seconds=1)

        self.assertEqual(set_status.call_args.args[3], "error")
        self.assertIn("GitHub rejected Steward's merge", upsert.call_args.args[3])
        self.assertIn("**Failed → Steward**", upsert.call_args.args[3])


if __name__ == "__main__":
    unittest.main()
