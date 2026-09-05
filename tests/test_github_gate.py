from __future__ import annotations

import unittest

from agent_factory.github_gate import _flatten_pages, _issue_is_valid_followup, _issue_numbers


class GithubGateAdapterTests(unittest.TestCase):
    def test_flattens_gh_slurp_pagination(self) -> None:
        self.assertEqual(_flatten_pages('[[{"id": 1}], [{"id": 2}]]'), [{"id": 1}, {"id": 2}])

    def test_extracts_local_issue_references(self) -> None:
        self.assertEqual(_issue_numbers("Tracks #12 and #45; ignore word#8"), {12, 45})

    def test_not_planned_issue_cannot_discharge_a_finding(self) -> None:
        self.assertFalse(_issue_is_valid_followup({"state": "CLOSED", "stateReason": "NOT_PLANNED"}))
        self.assertTrue(_issue_is_valid_followup({"state": "CLOSED", "stateReason": "COMPLETED"}))
        self.assertTrue(_issue_is_valid_followup({"state": "OPEN", "stateReason": None}))
