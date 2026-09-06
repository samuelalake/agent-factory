from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_factory.cli import default_config
from agent_factory.github_steward import format_status, run
from agent_factory.protocol import decode_data, encode_data


class StewardTests(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        path = root / "config.json"
        path.write_text(json.dumps(default_config("demo")))
        return path

    def test_status_has_human_and_machine_state(self) -> None:
        body = format_status("<!-- steward:test -->", "83", "dispatched", "Builder", "Ready.")
        self.assertIn("## Steward", body)
        self.assertIn("Dispatched → Builder", body)
        self.assertEqual(decode_data(body)["next_owner"], "builder")

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
