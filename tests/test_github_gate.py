from __future__ import annotations

import unittest
import json
from pathlib import Path
from unittest.mock import patch
from agent_factory.cli import default_config
from agent_factory.config import parse_config
from agent_factory.protocol import encode_data
from agent_factory.github_gate import evaluate_and_publish

from agent_factory.github_gate import (
    _flatten_pages,
    _followup_issue_numbers,
    _issue_is_valid_followup,
    _issue_numbers,
)


class GithubGateAdapterTests(unittest.TestCase):
    def test_flattens_gh_slurp_pagination(self) -> None:
        self.assertEqual(_flatten_pages('[[{"id": 1}], [{"id": 2}]]'), [{"id": 1}, {"id": 2}])

    def test_extracts_local_issue_references(self) -> None:
        self.assertEqual(_issue_numbers("Tracks #12 and #45; ignore word#8"), {12, 45})

    def test_only_explicit_reviewer_followup_can_discharge_debt(self) -> None:
        body = """Closes #83

<!-- agent-factory:review-followup-link -->
Reviewer follow-up: #91
"""
        self.assertEqual(_followup_issue_numbers(body), {91})
        self.assertNotIn(83, _followup_issue_numbers(body))

    def test_not_planned_issue_cannot_discharge_a_finding(self) -> None:
        self.assertFalse(_issue_is_valid_followup({"state": "CLOSED", "stateReason": "NOT_PLANNED"}))
        self.assertTrue(_issue_is_valid_followup({"state": "CLOSED", "stateReason": "COMPLETED"}))
        self.assertTrue(_issue_is_valid_followup({"state": "OPEN", "stateReason": None}))

    def test_only_configured_reviewer_on_exact_github_commit_can_clear_gate(self):
        config=parse_config(default_config('demo')); head='a'*40
        meta={'headRefOid':head,'mergeable':'MERGEABLE','statusCheckRollup':[],'body':''}
        for author,commit,declared,expected in [
            (config.review.app_login,head,head,'success'),
            ('unconfigured-reader',head,head,'pending'),
            (config.review.app_login,'b'*40,head,'pending'),
            (config.review.app_login,head,'b'*40,'pending'),
        ]:
            review={'user':{'login':author},'state':'APPROVED','commit_id':commit,
                    'body':config.review.marker+'\n'+encode_data({'head_sha':declared,'findings':[]})}
            with self.subTest(author=author,commit=commit,declared=declared), \
                 patch('agent_factory.github_gate.load_config',return_value=config), \
                 patch('agent_factory.github_gate._gh',side_effect=[json.dumps(meta),json.dumps([review]),'{}']):
                self.assertEqual(evaluate_and_publish('owner/repo','7',Path('unused')).state,expected)
