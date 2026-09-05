from __future__ import annotations

import unittest

from agent_factory.gate import Check, Finding, GateInput, Review, evaluate_gate


def subject(**overrides):
    data = {
        "mergeable": True,
        "head_sha": "abc",
        "checks": (Check("build", "success"),),
        "required_checks": ("build",),
        "review": Review("abc", "approve"),
    }
    data.update(overrides)
    return GateInput(**data)


class GateTests(unittest.TestCase):
    def test_priority_mergeability_before_checks(self) -> None:
        decision = evaluate_gate(subject(mergeable=False, checks=(Check("build", "failure"),)))
        self.assertEqual((decision.state, decision.code), ("failure", "merge-conflict"))

    def test_missing_required_check_is_pending(self) -> None:
        decision = evaluate_gate(subject(checks=()))
        self.assertEqual((decision.state, decision.code), ("pending", "checks"))

    def test_stale_review_is_pending(self) -> None:
        decision = evaluate_gate(subject(review=Review("old", "approve")))
        self.assertEqual(decision.code, "review-freshness")

    def test_p1_fails_even_with_approval(self) -> None:
        review = Review("abc", "approve", (Finding("P1", "src/a.py:1"),))
        self.assertEqual(evaluate_gate(subject(review=review)).code, "p1")

    def test_orphan_p2_fails(self) -> None:
        review = Review("abc", "approve", (Finding("P2", "src/a.py:2"),))
        self.assertEqual(evaluate_gate(subject(review=review)).code, "orphan-findings")

    def test_tracked_p2_passes(self) -> None:
        review = Review("abc", "approve", (Finding("P2", "src/a.py:2", tracked=True),))
        self.assertEqual(evaluate_gate(subject(review=review)).state, "success")
