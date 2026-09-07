from __future__ import annotations

import unittest

from agent_factory.github_review import (
    diff_right_lines,
    format_body,
    normalize_review,
    review_payload,
)
from agent_factory.protocol import decode_data


class ReviewTests(unittest.TestCase):
    def test_p1_overrides_model_approval(self) -> None:
        review = normalize_review({
            "approve": True,
            "summary": "looks good",
            "findings": [{"severity": "P1", "file": "x.py", "line": 3, "title": "breaks"}],
        })
        self.assertFalse(review["approve"])
        self.assertEqual(review["findings"][0]["path"], "x.py")
        self.assertEqual(review["findings"][0]["line"], 3)

    def test_body_carries_human_and_machine_contracts(self) -> None:
        review = normalize_review({"approve": True, "summary": "ok", "findings": []})
        body = format_body("<!-- reviewer:test -->", "abc", review, "gemini", "gemini-3.6-flash")
        self.assertTrue(body.startswith("<!-- reviewer:test -->"))
        self.assertIn("## Reviewer", body)
        self.assertIn("**Approved** for current head `abc`", body)
        self.assertIn("Findings: **P1 0 · P2 0 · P3 0**", body)
        self.assertIn("Model: `gemini/gemini-3.6-flash`", body)
        self.assertNotIn("### P1", body)
        self.assertEqual(decode_data(body)["head_sha"], "abc")

    def test_diff_right_lines_excludes_deleted_lines(self) -> None:
        diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -8,3 +8,4 @@
 context
-removed
+added
+another
"""
        self.assertEqual(diff_right_lines(diff), {"src/a.py": {8, 9, 10}})

    def test_payload_attaches_valid_findings_inline(self) -> None:
        diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,2 @@
 same
+risk = True
"""
        review = normalize_review({
            "approve": False,
            "summary": "One defect.",
            "findings": [{
                "severity": "P1",
                "file": "src/a.py",
                "line": 2,
                "title": "Unsafe default",
                "reasoning": "This enables the risky path.",
                "suggestion": "Default to false.",
            }],
        })
        payload = review_payload("<!-- reviewer:test -->", "abc", review, "gemini", "flash", diff)
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertEqual(payload["comments"], [{
            "path": "src/a.py",
            "line": 2,
            "side": "RIGHT",
            "body": "**[P1] Unsafe default**\n\nThis enables the risky path.\n\nSuggested change: Default to false.",
        }])
        self.assertIn("1 finding is attached inline", payload["body"])
        self.assertNotIn("Unsafe default", payload["body"])
        self.assertEqual(decode_data(payload["body"])["findings"], [{
            "severity": "P1",
            "key": "src/a.py:2",
            "title": "Unsafe default",
            "reasoning": "This enables the risky path.",
            "suggestion": "Default to false.",
        }])

    def test_payload_keeps_unanchorable_findings_in_summary(self) -> None:
        diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-old
+new
"""
        review = normalize_review({
            "approve": True,
            "summary": "Follow-up recommended.",
            "findings": [
                {"severity": "P2", "file": "src/a.py", "line": 99, "title": "Outside the diff"},
                {"severity": "P3", "title": "Repository-wide cleanup"},
            ],
        })
        payload = review_payload("<!-- reviewer:test -->", "abc", review, "gemini", "flash", diff)
        self.assertNotIn("comments", payload)
        self.assertIn("Outside the diff", payload["body"])
        self.assertIn("Repository-wide cleanup", payload["body"])
