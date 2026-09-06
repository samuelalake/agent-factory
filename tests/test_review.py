from __future__ import annotations

import unittest

from agent_factory.github_review import format_body, normalize_review
from agent_factory.protocol import decode_data


class ReviewTests(unittest.TestCase):
    def test_p1_overrides_model_approval(self) -> None:
        review = normalize_review({
            "approve": True,
            "summary": "looks good",
            "findings": [{"severity": "P1", "file": "x.py", "line": 3, "title": "breaks"}],
        })
        self.assertFalse(review["approve"])

    def test_body_carries_human_and_machine_contracts(self) -> None:
        review = normalize_review({"approve": True, "summary": "ok", "findings": []})
        body = format_body("<!-- reviewer:test -->", "abc", review, "gemini", "gemini-3.6-flash")
        self.assertTrue(body.startswith("<!-- reviewer:test -->"))
        self.assertIn("Reviewer: `gemini/gemini-3.6-flash`", body)
        self.assertEqual(decode_data(body)["head_sha"], "abc")
