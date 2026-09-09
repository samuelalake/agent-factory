from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_factory.cli import default_config
from agent_factory.github_steward import (
    SHAPED_MARKER,
    apply_shape,
    format_shaped_issue,
    format_status,
    normalize_shape,
    original_intake,
    run,
)
from agent_factory.protocol import decode_data, encode_data


class StewardTests(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        path = root / "config.json"
        path.write_text(json.dumps(default_config("demo")))
        return path

    def test_reusable_workflow_serializes_steward_per_issue(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github/workflows/steward.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "group: agent-factory-steward-${{ github.repository }}-${{ inputs.issue }}",
            workflow,
        )
        self.assertIn("cancel-in-progress: false", workflow)

    def test_status_has_human_and_machine_state(self) -> None:
        body = format_status(
            "<!-- steward:test -->", "83", "dispatched", "Builder", "Ready.",
            dispatched_after_builder_result_id="github-run:17:1",
        )
        self.assertIn("## Steward", body)
        self.assertIn("Dispatched → Builder", body)
        self.assertEqual(decode_data(body)["next_owner"], "builder")
        self.assertEqual(
            decode_data(body)["dispatched_after_builder_result_id"], "github-run:17:1"
        )

    def test_shape_contract_rejects_issue_explosion(self) -> None:
        raw = {
            "decision": "split",
            "title": "Parent",
            "outcome": "Deliver the feature in bounded slices.",
            "subtasks": [
                {"title": f"Slice {index}", "outcome": "Deliver it."}
                for index in range(4)
            ],
        }
        with self.assertRaisesRegex(ValueError, "max_subtasks"):
            normalize_shape(raw, 3)

    def test_shape_contract_requires_actionable_content(self) -> None:
        with self.assertRaisesRegex(ValueError, "title and outcome"):
            normalize_shape({"decision": "ready", "title": "", "outcome": ""}, 3)

    def test_non_contract_already_split_decision_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported Steward decision"):
            normalize_shape({
                "decision": "already-split",
                "title": "Proposed or existing children",
                "outcome": "Do not silently create duplicate issues.",
                "subtasks": [
                    {"title": "A child", "outcome": "Deliver one bounded result."},
                ],
            }, 3)

    def test_tracker_vocabulary_is_normalized_from_structured_intent(self) -> None:
        base = {"title": "Refine the delivery", "outcome": "Ship the corrected result."}

        for alias in ("keep-open", "keep_open", "update", "intake"):
            with self.subTest(alias=alias):
                self.assertEqual(
                    normalize_shape({**base, "decision": alias}, 3)["decision"],
                    "ready",
                )

        needs_human = normalize_shape({
            **base,
            "decision": "update",
            "questions": ["Which product behavior should win?"],
        }, 3)
        self.assertEqual(needs_human["decision"], "needs_human")

        duplicate = normalize_shape({
            **base,
            "decision": "intake",
            "duplicate_issue": 91,
        }, 3)
        self.assertEqual(duplicate["decision"], "duplicate")

        split = normalize_shape({
            **base,
            "decision": "keep-open",
            "subtasks": [{"title": "One slice", "outcome": "Deliver it."}],
        }, 3)
        self.assertEqual(split["decision"], "split")

    def test_repeated_shaping_preserves_original_intake(self) -> None:
        first = """<!-- agent-factory:steward-shaped -->

## Outcome

First brief.

<details>
<summary>Original intake</summary>

The user's rough report and product intent.

</details>
"""
        self.assertEqual(
            original_intake(first),
            "The user's rough report and product intent.",
        )
        reshaped = format_shaped_issue(
            normalize_shape({
                "decision": "needs_human",
                "title": "Clarify the flow",
                "outcome": "Second brief after new evidence.",
            }, 3),
            original_intake(first),
        )
        self.assertIn("Second brief after new evidence.", reshaped)
        self.assertIn("The user's rough report and product intent.", reshaped)

    @mock.patch("agent_factory.github_steward._gh")
    def test_split_is_bounded_and_child_creation_is_idempotent(self, gh) -> None:
        plan = normalize_shape({
            "decision": "split",
            "title": "Interaction work",
            "outcome": "Deliver two independent behaviors.",
            "subtasks": [
                {
                    "title": "First behavior",
                    "outcome": "Deliver the first behavior.",
                    "acceptance_criteria": ["Matches its reference."],
                    "verification": ["Capture evidence."],
                },
                {
                    "title": "Second behavior",
                    "outcome": "Deliver the second behavior.",
                },
            ],
        }, 3)
        inventory = [{
            "number": 90,
            "body": "<!-- agent-factory:steward-subtask parent=83 slot=1 -->",
        }]
        gh.side_effect = ["", json.dumps({"number": 91}), ""]
        state, _, detail = apply_shape(
            "owner/repo", "83", {"body": "rough intake"}, plan, inventory
        )
        self.assertEqual(state, "split")
        self.assertIn("2 bounded delivery slices", detail)
        self.assertEqual(gh.call_args_list[0].args[0][1], "repos/owner/repo/issues/90")
        self.assertEqual(gh.call_args_list[1].args[0][1], "repos/owner/repo/issues")
        parent_payload = json.loads(gh.call_args_list[2].kwargs["stdin"])
        self.assertIn(SHAPED_MARKER, parent_payload["body"])
        self.assertIn("- [ ] #90", parent_payload["body"])
        self.assertIn("- [ ] #91", parent_payload["body"])

    @mock.patch("agent_factory.github_steward._gh")
    def test_split_reuses_semantically_identical_open_issue(self, gh) -> None:
        outcome = "Define authenticated role-scoped agent mentions."
        plan = normalize_shape({
            "decision": "split",
            "title": "Factory follow-ups",
            "outcome": "Track one bounded planning slice.",
            "subtasks": [{
                "title": "Secure role-scoped mentions",
                "outcome": outcome,
            }],
        }, 3)
        inventory = [{
            "number": 141,
            "state": "OPEN",
            "title": "Secure role-scoped mentions",
            "body": f"## Outcome\n\n{outcome}\n",
        }]
        gh.return_value = ""

        state, _, detail = apply_shape(
            "owner/repo", "139", {"body": "rough intake"}, plan, inventory
        )

        self.assertEqual(state, "split")
        self.assertIn("1 bounded delivery slices", detail)
        self.assertEqual(len(gh.call_args_list), 1)
        parent_payload = json.loads(gh.call_args_list[0].kwargs["stdin"])
        self.assertIn("- [ ] #141", parent_payload["body"])

    @mock.patch("agent_factory.github_steward._gh")
    def test_split_never_reuses_parent_as_child(self, gh) -> None:
        outcome = "Create the bounded child."
        plan = normalize_shape({
            "decision": "split",
            "title": "Same title",
            "outcome": outcome,
            "subtasks": [{"title": "Same title", "outcome": outcome}],
        }, 3)
        gh.side_effect = [json.dumps({"number": 142}), ""]

        apply_shape(
            "owner/repo", "139", {"body": outcome}, plan, [{
                "number": 139,
                "state": "OPEN",
                "title": "Same title",
                "body": outcome,
            }]
        )

        self.assertEqual(gh.call_args_list[0].args[0][1], "repos/owner/repo/issues")

    @mock.patch("agent_factory.github_steward._gh")
    def test_split_fails_closed_on_ambiguous_or_repeated_reuse(self, gh) -> None:
        outcome = "Define the same bounded result."
        subtask = {"title": "Same slice", "outcome": outcome}
        one = {
            "number": 141, "state": "OPEN", "title": "Same slice", "body": outcome,
        }
        plan = normalize_shape({
            "decision": "split", "title": "Parent", "outcome": "Split safely.",
            "subtasks": [subtask],
        }, 3)
        with self.assertRaisesRegex(ValueError, "multiple open issues"):
            apply_shape("owner/repo", "139", {"body": "intake"}, plan, [
                one, {**one, "number": 142},
            ])

        repeated = normalize_shape({
            "decision": "split", "title": "Parent", "outcome": "Split safely.",
            "subtasks": [subtask, subtask],
        }, 3)
        with self.assertRaisesRegex(ValueError, "matches multiple"):
            apply_shape("owner/repo", "139", {"body": "intake"}, repeated, [one])
        gh.assert_not_called()

    def test_unready_issue_is_shaped_then_dispatched(self) -> None:
        calls: list[tuple[list[str], str | None]] = []
        plan = normalize_shape({
            "decision": "ready",
            "title": "Deliver drag interaction",
            "outcome": "Translate the next corpus pattern.",
            "acceptance_criteria": ["Match the reference states."],
            "verification": ["Publish current-head visual evidence."],
        }, 3)

        def fake_gh(args: list[str], *, stdin: str | None = None) -> str:
            calls.append((args, stdin))
            if args[:2] == ["issue", "view"]:
                return json.dumps({
                    "number": 83,
                    "state": "OPEN",
                    "title": "do next one",
                    "body": "rough intake",
                    "labels": [{"name": "agent:steward"}],
                })
            if args[:2] == ["api", "repos/owner/repo/issues?state=all&per_page=100"]:
                return "[]"
            if "/comments" in args[1] and "--paginate" in args:
                return "[]"
            return ""

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"GH_TOKEN": "steward-token"}, clear=True
        ), mock.patch("agent_factory.github_steward._gh", side_effect=fake_gh), mock.patch(
            "agent_factory.github_steward.shape_issue",
            return_value=(plan, "gemini", "flash"),
        ):
            state = run("owner/repo", "83", self._config(Path(tmp)))

        self.assertEqual(state, "dispatched")
        self.assertTrue(any(
            args[:2] == ["issue", "edit"] and "--add-label" in args and "ready" in args
            for args, _ in calls
        ))
        self.assertTrue(any(
            args[:2] == ["issue", "edit"] and "--add-label" in args and "agent:builder" in args
            for args, _ in calls
        ))
        parent_patch = next(
            json.loads(stdin)
            for args, stdin in calls
            if args[:2] == ["api", "repos/owner/repo/issues/83"] and stdin
        )
        self.assertEqual(parent_patch["title"], "Deliver drag interaction")
        self.assertIn("## Acceptance criteria", parent_patch["body"])
        self.assertTrue(any(
            args[:2] == ["api", "repos/owner/repo/issues?state=all&per_page=100"]
            and "--paginate" in args and "--slurp" in args
            for args, _ in calls
        ))

    def test_ready_issue_dispatches_builder_idempotently(self) -> None:
        calls: list[list[str]] = []

        def fake_gh(args: list[str], *, stdin: str | None = None) -> str:
            calls.append(args)
            if args[:2] == ["issue", "view"]:
                return json.dumps({"state": "OPEN", "labels": [{"name": "ready"}]})
            if "/comments" in args[1] and "--paginate" in args:
                return "[]"
            return ""

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"GH_TOKEN": "steward-token"}, clear=True
        ), mock.patch("agent_factory.github_steward._gh", side_effect=fake_gh):
            state = run("owner/repo", "83", self._config(Path(tmp)))

        self.assertEqual(state, "dispatched")
        created = [args[2] for args in calls if args[:2] == ["label", "create"]]
        self.assertIn("agent:steward", created)
        self.assertIn("agent:retry", created)
        self.assertIn("agent:builder", created)
        self.assertTrue(any("--add-label" in args and "agent:builder" in args for args in calls))

    def test_retry_does_not_duplicate_an_active_builder_dispatch(self) -> None:
        calls: list[list[str]] = []
        builder_data = encode_data({
            "version": 1, "role": "builder", "state": "complete",
            "result_id": "github-run:17:1",
        })

        def fake_gh(args: list[str], *, stdin: str | None = None) -> str:
            calls.append(args)
            if args[:2] == ["issue", "view"]:
                return json.dumps({
                    "state": "OPEN",
                    "labels": [
                        {"name": "ready"},
                        {"name": "agent:builder"},
                        {"name": "agent:retry"},
                    ],
                })
            if "/comments" in args[1] and "--paginate" in args:
                return json.dumps([{"id": 7, "body": builder_data}])
            return ""

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"GH_TOKEN": "steward-token"}, clear=True
        ), mock.patch("agent_factory.github_steward._gh", side_effect=fake_gh):
            state = run("owner/repo", "83", self._config(Path(tmp)))

        self.assertEqual(state, "dispatched")
        self.assertFalse(any(
            "--remove-label" in args and "agent:builder" in args for args in calls
        ))
        self.assertFalse(any(
            "--add-label" in args and "agent:builder" in args for args in calls
        ))
        self.assertTrue(any(
            "--remove-label" in args and "agent:retry" in args for args in calls
        ))

    def test_second_retry_does_not_duplicate_a_blocked_builder_redispatch(self) -> None:
        calls: list[list[str]] = []
        builder_data = encode_data({
            "version": 1, "role": "builder", "state": "blocked",
            "result_id": "github-run:17:1",
        })
        steward_data = encode_data({
            "version": 1,
            "role": "steward",
            "state": "dispatched",
            "dispatched_after_builder_result_id": "github-run:17:1",
        })

        def fake_gh(args: list[str], *, stdin: str | None = None) -> str:
            calls.append(args)
            if args[:2] == ["issue", "view"]:
                return json.dumps({
                    "state": "OPEN",
                    "labels": [
                        {"name": "ready"},
                        {"name": "agent:builder"},
                        {"name": "agent:retry"},
                    ],
                })
            if "/comments" in args[1] and "--paginate" in args:
                return json.dumps([
                    {"id": 7, "body": builder_data},
                    {"id": 8, "body": steward_data},
                ])
            return ""

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"GH_TOKEN": "steward-token"}, clear=True
        ), mock.patch("agent_factory.github_steward._gh", side_effect=fake_gh):
            state = run("owner/repo", "83", self._config(Path(tmp)))

        self.assertEqual(state, "dispatched")
        self.assertFalse(any(
            "--remove-label" in args and "agent:builder" in args for args in calls
        ))
        self.assertFalse(any(
            "--add-label" in args and "agent:builder" in args for args in calls
        ))
        self.assertTrue(any(
            "--remove-label" in args and "agent:retry" in args for args in calls
        ))

    def test_new_blocked_result_after_redispatch_can_be_retried(self) -> None:
        calls: list[list[str]] = []
        builder_data = encode_data({
            "version": 1, "role": "builder", "state": "blocked",
            "result_id": "github-run:18:1",
        })
        steward_data = encode_data({
            "version": 1,
            "role": "steward",
            "state": "dispatched",
            "dispatched_after_builder_result_id": "github-run:17:1",
        })

        def fake_gh(args: list[str], *, stdin: str | None = None) -> str:
            calls.append(args)
            if args[:2] == ["issue", "view"]:
                return json.dumps({
                    "state": "OPEN",
                    "labels": [
                        {"name": "ready"},
                        {"name": "agent:builder"},
                        {"name": "agent:retry"},
                    ],
                })
            if "/comments" in args[1] and "--paginate" in args:
                return json.dumps([
                    {"id": 7, "body": builder_data},
                    {"id": 8, "body": steward_data},
                ])
            return ""

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"GH_TOKEN": "steward-token"}, clear=True
        ), mock.patch("agent_factory.github_steward._gh", side_effect=fake_gh):
            state = run("owner/repo", "83", self._config(Path(tmp)))

        self.assertEqual(state, "dispatched")
        remove_index = next(
            index for index, args in enumerate(calls)
            if "--remove-label" in args and "agent:builder" in args
        )
        add_index = next(
            index for index, args in enumerate(calls)
            if "--add-label" in args and "agent:builder" in args
        )
        self.assertLess(remove_index, add_index)

    def test_blocked_builder_waits_until_retry_label(self) -> None:
        builder_data = encode_data({"version": 1, "role": "builder", "state": "blocked"})

        def exercise(labels: list[str]) -> tuple[str, list[list[str]]]:
            calls: list[list[str]] = []

            def fake_gh(args: list[str], *, stdin: str | None = None) -> str:
                calls.append(args)
                if args[:2] == ["issue", "view"]:
                    return json.dumps({"state": "OPEN", "labels": [{"name": x} for x in labels]})
                if "/comments" in args[1] and "--paginate" in args:
                    return json.dumps([{"id": 7, "body": builder_data}])
                return ""

            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                "os.environ", {"GH_TOKEN": "steward-token"}, clear=True
            ), mock.patch("agent_factory.github_steward._gh", side_effect=fake_gh):
                state = run("owner/repo", "83", self._config(Path(tmp)))
            return state, calls

        blocked_state, blocked_calls = exercise(["ready", "agent:steward"])
        retry_state, retry_calls = exercise(["ready", "agent:steward", "agent:retry"])
        self.assertEqual(blocked_state, "blocked")
        self.assertFalse(
            any("--add-label" in args and "agent:builder" in args for args in blocked_calls)
        )
        self.assertEqual(retry_state, "dispatched")
        self.assertTrue(any(args[:2] == ["label", "create"] for args in retry_calls))

        redispatch_state, redispatch_calls = exercise(
            ["ready", "agent:steward", "agent:retry", "agent:builder"]
        )
        self.assertEqual(redispatch_state, "dispatched")
        remove_index = next(
            index
            for index, args in enumerate(redispatch_calls)
            if "--remove-label" in args and "agent:builder" in args
        )
        add_index = next(
            index
            for index, args in enumerate(redispatch_calls)
            if "--add-label" in args and "agent:builder" in args
        )
        self.assertLess(remove_index, add_index)


if __name__ == "__main__":
    unittest.main()
