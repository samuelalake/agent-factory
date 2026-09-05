"""Pure, priority-ordered merge-gate state machine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

GateState = Literal["success", "failure", "pending"]


@dataclass(frozen=True)
class Check:
    name: str
    state: str


@dataclass(frozen=True)
class Finding:
    severity: str
    key: str
    tracked: bool = False


@dataclass(frozen=True)
class Review:
    head_sha: str
    verdict: Literal["approve", "request_changes", "comment"]
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True)
class GateInput:
    mergeable: bool | None
    head_sha: str
    checks: tuple[Check, ...]
    required_checks: tuple[str, ...]
    review: Review | None


@dataclass(frozen=True)
class GateDecision:
    state: GateState
    code: str
    description: str


PASSING_CHECK_STATES = {"success", "skipped", "neutral"}


def evaluate_gate(value: GateInput) -> GateDecision:
    """Return the first applicable decision in the documented priority order."""
    if value.mergeable is False:
        return GateDecision("failure", "merge-conflict", "branch has merge conflicts")
    if value.mergeable is None:
        return GateDecision("pending", "mergeability", "waiting for mergeability")

    by_name = {check.name: check.state.lower() for check in value.checks}
    for name in value.required_checks:
        state = by_name.get(name)
        if state is None or state in {"pending", "queued", "in_progress"}:
            return GateDecision("pending", "checks", f"waiting for check {name}")
        if state not in PASSING_CHECK_STATES:
            return GateDecision("failure", "checks", f"check {name} is {state}")

    review = value.review
    if review is None or review.head_sha != value.head_sha:
        return GateDecision("pending", "review-freshness", "waiting for current-head review")
    if review.verdict == "request_changes":
        return GateDecision("failure", "review-verdict", "reviewer requested changes")
    if review.verdict != "approve":
        return GateDecision("pending", "review-verdict", "waiting for reviewer approval")

    p1 = [finding for finding in review.findings if finding.severity.upper() == "P1"]
    if p1:
        return GateDecision("failure", "p1", f"{len(p1)} unresolved P1 finding(s)")

    orphans = [
        finding
        for finding in review.findings
        if finding.severity.upper() in {"P2", "P3"} and not finding.tracked
    ]
    if orphans:
        sample = ", ".join(finding.key for finding in orphans[:3])
        return GateDecision(
            "failure",
            "orphan-findings",
            f"{len(orphans)} untracked P2/P3 finding(s): {sample}",
        )

    return GateDecision("success", "all-clear", "all required evidence is current and clear")
