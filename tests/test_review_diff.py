from __future__ import annotations

import json
import unittest
from unittest import mock

from agent_factory.cli import default_config
from agent_factory.config import parse_config
from agent_factory.github_review import (
    _complete_patch,
    diff_right_lines,
    normalize_review,
    pull_request_diff,
    run,
)


def file_entry(path: str, **overrides) -> dict:
    return {
        "filename": path, "status": "modified", "additions": 1, "deletions": 1,
        "patch": "@@ -1 +1 @@\n-old\n+new", **overrides,
    }


class ReviewDiffTests(unittest.TestCase):
    def collect(self, entries, *, expected=None, current=None):
        meta = {
            "headRefOid": "a" * 40, "baseRefOid": "b" * 40,
            "changedFiles": len(entries) if expected is None else expected,
        }
        pages = [entries[i:i + 100] for i in range(0, len(entries), 100)]
        with mock.patch("agent_factory.github_review._gh", side_effect=[
            json.dumps(pages), json.dumps(meta if current is None else current),
        ]) as gh:
            result = pull_request_diff("o/r", "106", meta)
        self.assertEqual(gh.call_args_list[0].args[0], [
            "api", "repos/o/r/pulls/106/files?per_page=100", "--paginate", "--slurp",
        ])
        return result

    def run_review(self, entries, *, expected=None, limit=1_000_000, body="", delivery=None):
        meta = {
            "headRefOid": "a" * 40, "baseRefOid": "b" * 40,
            "changedFiles": len(entries) if expected is None else expected,
            "title": "Large PR", "body": body,
        }
        raw_config = default_config("fixture")
        raw_config["review"]["max_diff_bytes"] = limit
        raw_config["review"]["require_builder_delivery"] = delivery is not None
        raw_config["review"]["visual_evidence_paths"] = []
        config = parse_config(raw_config)
        posted = []

        def gh(args, *, stdin=None):
            if args[:2] == ["pr", "view"]:
                return json.dumps(meta)
            if args[:2] == ["api", "repos/o/r/pulls/106/files?per_page=100"]:
                self.assertIn("--paginate", args)
                self.assertIn("--slurp", args)
                return json.dumps([entries[i:i + 100] for i in range(0, len(entries), 100)])
            if args[:2] == ["api", "repos/o/r/pulls/106/reviews"]:
                posted.append(json.loads(stdin))
                return ""
            # A regression to gh pr diff must fail, even in deterministic gates.
            raise AssertionError(args)

        review = normalize_review({"summary": "Reviewed", "findings": [{
            "severity": "P2", "file": entries[-1]["filename"], "line": 1,
            "title": "Advisory on final page",
        }]})
        with (
            mock.patch("agent_factory.github_review.get_installation_token", return_value="token"),
            mock.patch("agent_factory.github_review.load_config", return_value=config),
            mock.patch("agent_factory.github_review.discover_context", return_value=[]),
            mock.patch("agent_factory.github_review.wait_for_delivery", return_value=(delivery, body)),
            mock.patch("agent_factory.github_review._gh", side_effect=gh),
            mock.patch("agent_factory.github_review.request_review", return_value=(review, "test", "test")) as request,
        ):
            run("o/r", "106", mock.MagicMock(), mock.MagicMock())
        self.assertEqual(len(posted), 1)
        return posted[0], request

    def test_469_files_reach_model_and_last_page_findings_stay_inline(self):
        entries = [file_entry(f"src/file-{i}.py") for i in range(469)]
        payload, request = self.run_review(entries)
        prompt = request.call_args.args[2]
        self.assertEqual(prompt.count("diff --git "), 469)
        self.assertIn("+++ b/src/file-468.py", prompt)
        self.assertEqual(payload["event"], "APPROVE")
        self.assertEqual(payload["commit_id"], "a" * 40)
        self.assertEqual(payload["comments"][0]["path"], "src/file-468.py")
        self.assertEqual(payload["comments"][0]["line"], 1)

    def test_added_deleted_renamed_and_special_paths(self):
        special = 'space dir/é"tab\tand\nnewline.py'
        entries = [
            file_entry("add", status="added", deletions=0, patch="@@ -0,0 +1 @@\n+new"),
            file_entry("delete", status="removed", additions=0, patch="@@ -1 +0,0 @@\n-old"),
            file_entry(special, status="renamed", previous_filename="old name.py"),
        ]
        diff, missing = self.collect(entries)
        self.assertFalse(missing)
        self.assertIn("rename from \"old name.py\"", diff)
        self.assertIn("--- /dev/null", diff)
        self.assertIn("+++ /dev/null", diff)
        self.assertEqual(diff_right_lines(diff), {"add": {1}, special: {1}})

    def test_hunk_lengths_counts_multiple_hunks_and_no_newline(self):
        patch = "@@ -1,2 +1,2 @@\n context\n-old\n+new\n@@ -10 +10 @@\n-last\n\\ No newline at end of file\n+end\n\\ No newline at end of file"
        self.assertTrue(_complete_patch(patch, 2, 2))
        self.assertFalse(_complete_patch(patch, 3, 2))
        self.assertFalse(_complete_patch("@@ -1,2 +1,2 @@\n-old\n+new", 1, 1))
        self.assertFalse(_complete_patch("@@ -1 +1 @@\n-old\n+new\n+extra", 2, 1))
        self.assertFalse(_complete_patch("@@ -1 +1 @@\n-old", 0, 1))

    def test_absent_empty_binary_and_truncated_patches_cannot_approve(self):
        for overrides in (
            {"patch": None}, {"patch": ""},
            {"patch": None, "additions": 0, "deletions": 0},
            {"patch": "@@ -1 +1,2 @@\n-old\n+new"},
            {"additions": 2},
        ):
            with self.subTest(overrides=overrides):
                entries = [file_entry("last.png", **overrides)]
                payload, request = self.run_review(entries)
                self.assertEqual(payload["event"], "REQUEST_CHANGES")
                self.assertNotIn("comments", payload)
                self.assertIn("incomplete patch coverage", payload["body"])
                self.assertIn("last.png", payload["body"])
                self.assertIn("Incomplete patch coverage", request.call_args.args[2])

    def test_many_omitted_patches_keep_review_and_prompt_bounded(self):
        entries = [file_entry(f"assets/{i:04d}-" + "x" * 100 + ".png", patch=None) for i in range(469)]
        payload, request = self.run_review(entries)
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertIn("469 coverage gap(s)", payload["body"])
        self.assertIn("Further coverage details omitted", payload["body"])
        self.assertLess(len(payload["body"]), 10000)
        self.assertIn("469 coverage gap(s)", request.call_args.args[2])

    def test_added_text_resembling_file_header_keeps_real_inline_path(self):
        diff, missing = self.collect([file_entry(
            "actual.txt", status="added", additions=2, deletions=0,
            patch="@@ -0,0 +1,2 @@\n+++ b/not-a-file\n+second line",
        )])
        self.assertFalse(missing)
        self.assertEqual(diff_right_lines(diff), {"actual.txt": {1, 2}})

    def test_api_file_cap_is_reported_not_silent(self):
        payload, request = self.run_review(
            [file_entry(f"file-{i}") for i in range(3000)], expected=3001,
        )
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertIn("3000 of 3001", payload["body"])
        self.assertIn("3000 of 3001", request.call_args.args[2])

    def test_byte_limit_still_blocks_and_is_disclosed_to_model(self):
        payload, request = self.run_review([file_entry("file")], limit=30)
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertIn("Reviewer input omitted", payload["body"])
        self.assertIn("Configured input limit omitted", request.call_args.args[2])

    def test_pending_delivery_gate_does_not_fetch_whole_diff(self):
        body = default_config("fixture")["builder"]["marker"]
        payload, request = self.run_review([file_entry("file")], body=body, delivery="pending")
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        request.assert_not_called()

    def test_moving_head_base_or_file_count_discards_snapshot(self):
        meta = {"headRefOid": "a" * 40, "baseRefOid": "b" * 40, "changedFiles": 1}
        for key, value in (("headRefOid", "c" * 40), ("baseRefOid", "c" * 40), ("changedFiles", 2)):
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, "changed while collecting"):
                self.collect([file_entry("file")], current={**meta, key: value})

    def test_duplicate_malformed_pages_and_unknown_counts_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            self.collect([file_entry("same"), file_entry("same")])
        for pages in ({}, [{}], [[None]], [[{"filename": ""}]]):
            with self.subTest(pages=pages), mock.patch("agent_factory.github_review._gh", return_value=json.dumps(pages)):
                with self.assertRaisesRegex(RuntimeError, "invalid"):
                    pull_request_diff("o/r", "1", {"changedFiles": 1})
        with mock.patch("agent_factory.github_review._gh", return_value="[]"):
            with self.assertRaisesRegex(RuntimeError, "changed-file count"):
                pull_request_diff("o/r", "1", {})


if __name__ == "__main__":
    unittest.main()
