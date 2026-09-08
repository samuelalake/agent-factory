from __future__ import annotations

import json
import unittest
from unittest import mock

from agent_factory.cli import default_config
from agent_factory.config import parse_config
from agent_factory.github_builder import BuilderBlocked
from agent_factory.github_review import (
    diff_right_lines,
    failed_review,
    failed_delivery_review,
    format_body,
    normalize_review,
    request_review,
    repository_glob_match,
    review_payload,
    run,
)
from agent_factory.protocol import decode_data


class ReviewTests(unittest.TestCase):
    def test_repository_glob_matches_root_and_nested_double_star_paths(self) -> None:
        self.assertTrue(repository_glob_match("Demo.origami", "**/*.origami"))
        self.assertTrue(repository_glob_match("fixtures/Demo.origami", "**/*.origami"))
        self.assertTrue(repository_glob_match("foo/bar", "foo/**/bar"))
        self.assertTrue(repository_glob_match("foo/one/two/bar", "foo/**/bar"))
        self.assertFalse(repository_glob_match("Demo.swift", "**/*.origami"))

    def test_ready_delivery_without_trusted_images_cannot_be_approved(self) -> None:
        raw_config = default_config("fixture")
        raw_config["review"].update({
            "require_builder_delivery": True,
            "visual_evidence": True,
            "visual_evidence_paths": ["app/**"],
        })
        config = parse_config(raw_config)
        head = "a" * 40
        body = "\n".join([
            config.builder.marker,
            "<!-- agent-factory:builder-delivery:start -->",
            f"<!-- agent-factory:builder-delivery-head:{head} -->",
            "### Interaction_Drag",
            "normalized SSIM 0.99 · catastrophic sanity **pass**",
            "<!-- agent-factory:builder-delivery:end -->",
        ])
        posted: list[dict] = []

        def gh(args, *, stdin=None):
            if args[:2] == ["pr", "view"]:
                return json.dumps({"headRefOid": head, "title": "Drag", "body": body})
            if args[:2] == ["pr", "diff"]:
                return ""
            if args[0] == "api":
                posted.append(json.loads(stdin))
                return ""
            raise AssertionError(args)

        with (
            mock.patch("agent_factory.github_review.get_installation_token", return_value="token"),
            mock.patch("agent_factory.github_review.load_config", return_value=config),
            mock.patch("agent_factory.github_review.wait_for_delivery", return_value=("ready", body)),
            mock.patch("agent_factory.github_review._fetch_delivery_images") as fetch,
            mock.patch("agent_factory.github_review.discover_context", return_value=[]),
            mock.patch(
                "agent_factory.github_review.request_review",
                return_value=(normalize_review({"summary": "approve", "approve": True}), "openrouter", "visual"),
            ),
            mock.patch("agent_factory.github_review._gh", side_effect=gh),
        ):
            run("acme/repo", "7", mock.MagicMock(), mock.MagicMock())
        fetch.assert_not_called()
        self.assertEqual(posted[0]["event"], "REQUEST_CHANGES")
        self.assertIn(config.review.failure_marker, posted[0]["body"])
        self.assertIn("no trusted current-head visual evidence", posted[0]["body"])

    def test_ready_delivery_images_are_sent_for_fidelity_review(self) -> None:
        raw_config = default_config("fixture")
        raw_config["review"].update({
            "require_builder_delivery": True,
            "visual_evidence": True,
            "visual_evidence_paths": ["app/**"],
        })
        config = parse_config(raw_config)
        head = "a" * 40
        evidence_commit = "b" * 40
        body = "\n".join([
            config.builder.marker,
            "<!-- agent-factory:builder-delivery:start -->",
            f"<!-- agent-factory:builder-delivery-head:{head} -->",
            "### Interaction_Drag",
            "normalized SSIM 0.99 · catastrophic sanity **pass**",
            f"![Swami](https://github.com/acme/repo/raw/{evidence_commit}/pr-7/{head}/swami.png)",
            "<!-- agent-factory:builder-delivery:end -->",
        ])

        def gh(args, *, stdin=None):
            if args[:2] == ["pr", "view"]:
                return json.dumps({"headRefOid": head, "title": "Drag", "body": body})
            if args[:2] == ["pr", "diff"] or args[0] == "api":
                return ""
            raise AssertionError(args)

        with (
            mock.patch("agent_factory.github_review.get_installation_token", return_value="token"),
            mock.patch("agent_factory.github_review.load_config", return_value=config),
            mock.patch("agent_factory.github_review.wait_for_delivery", return_value=("ready", body)),
            mock.patch(
                "agent_factory.github_review._fetch_delivery_images",
                return_value=("data:image/png;base64,aGVsbG8=",),
            ),
            mock.patch("agent_factory.github_review.discover_context", return_value=[]),
            mock.patch(
                "agent_factory.github_review.request_review",
                return_value=(normalize_review({"summary": "match", "approve": True}), "openrouter", "visual"),
            ) as request,
            mock.patch("agent_factory.github_review._gh", side_effect=gh),
        ):
            run("acme/repo", "7", mock.MagicMock(), mock.MagicMock())
        self.assertEqual(request.call_args.kwargs["image_urls"], ("data:image/png;base64,aGVsbG8=",))

    def test_unreadable_ready_visual_evidence_cannot_be_approved(self) -> None:
        raw_config = default_config("fixture")
        raw_config["review"].update({
            "require_builder_delivery": True,
            "visual_evidence": True,
        })
        config = parse_config(raw_config)
        head = "a" * 40
        body = "\n".join([
            config.builder.marker,
            "<!-- agent-factory:builder-delivery:start -->",
            f"<!-- agent-factory:builder-delivery-head:{head} -->",
            f"![Swami](https://github.com/acme/repo/raw/{'b' * 40}/pr-7/{head}/swami.png)",
            "<!-- agent-factory:builder-delivery:end -->",
        ])
        posted: list[dict] = []

        def gh(args, *, stdin=None):
            if args[:2] == ["pr", "view"]:
                return json.dumps({"headRefOid": head, "title": "Drag", "body": body})
            if args[:2] == ["pr", "diff"]:
                return ""
            if args[0] == "api":
                posted.append(json.loads(stdin))
                return ""
            raise AssertionError(args)

        with (
            mock.patch("agent_factory.github_review.get_installation_token", return_value="token"),
            mock.patch("agent_factory.github_review.load_config", return_value=config),
            mock.patch("agent_factory.github_review.wait_for_delivery", return_value=("ready", body)),
            mock.patch(
                "agent_factory.github_review._fetch_delivery_images",
                side_effect=BuilderBlocked("private detail"),
            ),
            mock.patch("agent_factory.github_review.discover_context", return_value=[]),
            mock.patch(
                "agent_factory.github_review.request_review",
                return_value=(normalize_review({"summary": "approve", "approve": True}), "openrouter", "visual"),
            ),
            mock.patch("agent_factory.github_review._gh", side_effect=gh),
        ):
            run("acme/repo", "7", mock.MagicMock(), mock.MagicMock())
        self.assertEqual(posted[0]["event"], "REQUEST_CHANGES")
        self.assertIn(config.review.failure_marker, posted[0]["body"])
        self.assertNotIn("private detail", posted[0]["body"])

    def test_failed_delivery_is_visually_reviewed_but_cannot_be_approved(self) -> None:
        raw_config = default_config("fixture")
        raw_config["review"].update({
            "require_builder_delivery": True,
            "visual_evidence": True,
        })
        config = parse_config(raw_config)
        head = "a" * 40
        evidence_commit = "b" * 40
        body = "\n".join([
            config.builder.marker,
            "<!-- agent-factory:builder-delivery:start -->",
            f"<!-- agent-factory:builder-delivery-head:{head} -->",
            "### Interaction_Drag",
            "normalized SSIM 0.01 · catastrophic sanity **fail**",
            f"![Swami](https://github.com/acme/repo/raw/{evidence_commit}/pr-7/{head}/swami.png)",
            "<!-- agent-factory:builder-delivery:end -->",
        ])
        posted: list[dict] = []

        def gh(args, *, stdin=None):
            if args[:2] == ["pr", "view"]:
                return json.dumps({"headRefOid": head, "title": "Drag", "body": body})
            if args[:2] == ["pr", "diff"]:
                return ""
            if args[:2] == ["api", "repos/acme/repo/pulls/7/reviews"]:
                posted.append(json.loads(stdin))
                return ""
            raise AssertionError(args)

        with (
            mock.patch("agent_factory.github_review.get_installation_token", return_value="token"),
            mock.patch("agent_factory.github_review.load_config", return_value=config),
            mock.patch("agent_factory.github_review.wait_for_delivery", return_value=("failed", body)),
            mock.patch(
                "agent_factory.github_review._fetch_delivery_images",
                return_value=("data:image/png;base64,aGVsbG8=",),
            ),
            mock.patch("agent_factory.github_review.discover_context", return_value=[]),
            mock.patch(
                "agent_factory.github_review.request_review",
                return_value=(
                    normalize_review({"summary": "Model approves.", "approve": True, "findings": []}),
                    "openrouter",
                    "visual",
                ),
            ) as request,
            mock.patch("agent_factory.github_review._gh", side_effect=gh),
        ):
            run("acme/repo", "7", mock.MagicMock(), mock.MagicMock())

        self.assertEqual(request.call_args.kwargs["image_urls"], ("data:image/png;base64,aGVsbG8=",))
        self.assertEqual(posted[0]["event"], "REQUEST_CHANGES")
        machine = decode_data(posted[0]["body"])
        self.assertEqual(machine["verdict"], "request_changes")
        self.assertEqual(machine["findings"][0]["severity"], "P1")

    def test_pending_delivery_never_invokes_model(self) -> None:
        raw_config = default_config("fixture")
        raw_config["review"]["require_builder_delivery"] = True
        config = parse_config(raw_config)
        body = config.builder.marker

        def gh(args, *, stdin=None):
            if args[:2] == ["pr", "view"]:
                return json.dumps({"headRefOid": "a" * 40, "title": "Drag", "body": body})
            if args[:2] == ["pr", "diff"] or args[0] == "api":
                return ""
            raise AssertionError(args)

        with (
            mock.patch("agent_factory.github_review.get_installation_token", return_value="token"),
            mock.patch("agent_factory.github_review.load_config", return_value=config),
            mock.patch("agent_factory.github_review.wait_for_delivery", return_value=("pending", body)),
            mock.patch("agent_factory.github_review.request_review") as request,
            mock.patch("agent_factory.github_review._gh", side_effect=gh),
        ):
            run("acme/repo", "7", mock.MagicMock(), mock.MagicMock())
        request.assert_not_called()

    def test_non_builder_control_plane_review_does_not_require_visual_artifacts(self) -> None:
        raw_config = default_config("fixture")
        raw_config["review"].update({
            "require_builder_delivery": True,
            "visual_evidence": True,
            "visual_evidence_paths": ["app/**"],
        })
        config = parse_config(raw_config)
        head = "a" * 40
        posted: list[dict] = []

        def gh(args, *, stdin=None):
            if args[:2] == ["pr", "view"]:
                return json.dumps({
                    "headRefOid": head,
                    "title": "Pin reviewed workflow runtime",
                    "body": "Control-plane promotion with linked prior evidence.",
                })
            if args[:2] == ["api", "repos/acme/repo/pulls/7/files?per_page=100"]:
                return json.dumps([[{"filename": ".github/workflows/review.yml"}]])
            if args[:2] == ["pr", "diff"]:
                return """diff --git a/.github/workflows/review.yml b/.github/workflows/review.yml
--- a/.github/workflows/review.yml
+++ b/.github/workflows/review.yml
@@ -1 +1 @@
-uses: owner/factory@old
+uses: owner/factory@new
"""
            if args[:2] == ["api", "repos/acme/repo/pulls/7/reviews"]:
                posted.append(json.loads(stdin))
                return ""
            raise AssertionError(args)

        with (
            mock.patch("agent_factory.github_review.get_installation_token", return_value="token"),
            mock.patch("agent_factory.github_review.load_config", return_value=config),
            mock.patch("agent_factory.github_review.wait_for_delivery") as wait,
            mock.patch("agent_factory.github_review.discover_context", return_value=[]),
            mock.patch(
                "agent_factory.github_review.request_review",
                return_value=(
                    normalize_review({"summary": "Control plane is sound.", "approve": True}),
                    "openrouter",
                    "visual",
                ),
            ) as request,
            mock.patch("agent_factory.github_review._gh", side_effect=gh),
        ):
            run("acme/repo", "7", mock.MagicMock(), mock.MagicMock())

        wait.assert_not_called()
        self.assertEqual(request.call_args.kwargs["image_urls"], ())
        system = request.call_args.args[1]
        user = request.call_args.args[2]
        self.assertIn("do not request a screenshot triplet", system)
        self.assertIn("must identify a concrete defect", system)
        self.assertIn("immutable referenced reusable workflow", system)
        self.assertIn("absence of dependency source code", system)
        self.assertIn("absence of a triplet is not a finding", user)
        self.assertEqual(posted[0]["event"], "APPROVE")

    def test_non_builder_visual_change_without_evidence_is_deterministically_blocked(self) -> None:
        raw_config = default_config("fixture")
        raw_config["review"].update({
            "require_builder_delivery": True,
            "visual_evidence": True,
            "visual_evidence_paths": ["app/**", "**/*.origami"],
        })
        config = parse_config(raw_config)
        head = "a" * 40
        posted: list[dict] = []

        def gh(args, *, stdin=None):
            if args[:2] == ["pr", "view"]:
                return json.dumps({
                    "headRefOid": head,
                    "title": "Change visual surface",
                    "body": "Human-authored visual update without Builder evidence.",
                })
            if args[:2] == ["api", "repos/acme/repo/pulls/7/files?per_page=100"]:
                return json.dumps([[{"filename": "app/Screen.swift"}]])
            if args[:2] == ["pr", "diff"]:
                return """diff --git a/app/Screen.swift b/app/Screen.swift
--- a/app/Screen.swift
+++ b/app/Screen.swift
@@ -1 +1 @@
-let color = old
+let color = new
"""
            if args[:2] == ["api", "repos/acme/repo/pulls/7/reviews"]:
                posted.append(json.loads(stdin))
                return ""
            raise AssertionError(args)

        with (
            mock.patch("agent_factory.github_review.get_installation_token", return_value="token"),
            mock.patch("agent_factory.github_review.load_config", return_value=config),
            mock.patch("agent_factory.github_review.wait_for_delivery") as wait,
            mock.patch("agent_factory.github_review.discover_context", return_value=[]),
            mock.patch(
                "agent_factory.github_review.request_review",
                return_value=(
                    normalize_review({"summary": "Model approves.", "approve": True}),
                    "openrouter",
                    "visual",
                ),
            ),
            mock.patch("agent_factory.github_review._gh", side_effect=gh),
        ):
            run("acme/repo", "7", mock.MagicMock(), mock.MagicMock())

        wait.assert_not_called()
        self.assertEqual(posted[0]["event"], "REQUEST_CHANGES")
        machine = decode_data(posted[0]["body"])
        self.assertEqual(machine["verdict"], "request_changes")
        self.assertEqual(machine["findings"][0]["severity"], "P1")
        self.assertIn("Visual change lacks current-head evidence", posted[0]["body"])

    def test_visual_path_after_first_hundred_files_or_before_rename_is_blocked(self) -> None:
        raw_config = default_config("fixture")
        raw_config["review"].update({
            "require_builder_delivery": True,
            "visual_evidence": True,
            "visual_evidence_paths": ["app/**"],
        })
        config = parse_config(raw_config)
        posted: list[dict] = []
        first_page = [{"filename": f"docs/note-{index}.md"} for index in range(100)]
        second_page = [{
            "filename": "archive/OldScreen.swift",
            "previous_filename": "app/OldScreen.swift",
        }]

        def gh(args, *, stdin=None):
            if args[:2] == ["pr", "view"]:
                return json.dumps({
                    "headRefOid": "a" * 40,
                    "title": "Large renamed visual change",
                    "body": "No Builder delivery.",
                })
            if args[:2] == ["api", "repos/acme/repo/pulls/7/files?per_page=100"]:
                self.assertIn("--paginate", args)
                self.assertIn("--slurp", args)
                return json.dumps([first_page, second_page])
            if args[:2] == ["pr", "diff"]:
                return ""
            if args[:2] == ["api", "repos/acme/repo/pulls/7/reviews"]:
                posted.append(json.loads(stdin))
                return ""
            raise AssertionError(args)

        with (
            mock.patch("agent_factory.github_review.get_installation_token", return_value="token"),
            mock.patch("agent_factory.github_review.load_config", return_value=config),
            mock.patch("agent_factory.github_review.wait_for_delivery") as wait,
            mock.patch("agent_factory.github_review.discover_context", return_value=[]),
            mock.patch(
                "agent_factory.github_review.request_review",
                return_value=(normalize_review({"summary": "Model approves.", "approve": True}), "openrouter", "visual"),
            ),
            mock.patch("agent_factory.github_review._gh", side_effect=gh),
        ):
            run("acme/repo", "7", mock.MagicMock(), mock.MagicMock())

        wait.assert_not_called()
        self.assertEqual(posted[0]["event"], "REQUEST_CHANGES")
        self.assertIn("app/OldScreen.swift", posted[0]["body"])

    def test_malformed_primary_reply_falls_back_to_structured_review(self) -> None:
        with (
            mock.patch(
                "agent_factory.github_review.complete",
                side_effect=["not json", '{"summary":"caught it","approve":false,"findings":[]}'],
            ) as complete,
            mock.patch.dict(
                "os.environ",
                {"OPENROUTER_API_KEY": "one", "NVIDIA_API_KEY": "two"},
                clear=True,
            ),
        ):
            review, provider, model = request_review(
                [("openrouter", "free", False), ("nvidia", "kimi", False)], "system", "user"
            )
        self.assertFalse(review["approve"])
        self.assertEqual((provider, model), ("nvidia", "kimi"))
        self.assertEqual(complete.call_count, 2)
        self.assertEqual(complete.call_args.kwargs["image_urls"], ())

    def test_review_passes_current_head_images_to_provider(self) -> None:
        with (
            mock.patch(
                "agent_factory.github_review.complete",
                return_value='{"summary":"visible mismatch","approve":false,"findings":[]}',
            ) as complete,
            mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "one"}, clear=True),
        ):
            request_review(
                [("openrouter", "visual", True)],
                "system",
                "user",
                image_urls=("data:image/png;base64,aGVsbG8=",),
            )
        self.assertEqual(
            complete.call_args.kwargs["image_urls"],
            ("data:image/png;base64,aGVsbG8=",),
        )

    def test_review_skips_text_only_route_when_images_are_present(self) -> None:
        with (
            mock.patch(
                "agent_factory.github_review.complete",
                return_value='{"summary":"seen","approve":false,"findings":[]}',
            ) as complete,
            mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "one"}, clear=True),
        ):
            _, provider, _ = request_review(
                [
                    ("minimax", "text-only", False),
                    ("openrouter", "visual", True),
                ],
                "system",
                "user",
                image_urls=("data:image/png;base64,aGVsbG8=",),
            )
        self.assertEqual(provider, "openrouter")
        self.assertEqual(complete.call_count, 1)

    def test_exhausted_providers_become_a_structured_blocking_review(self) -> None:
        review = failed_review("invalid JSON")
        self.assertFalse(review["approve"])
        self.assertEqual(review["findings"][0]["severity"], "P1")
        self.assertEqual(review["findings"][0]["key"], "review-wide")
        self.assertIn("invalid JSON", review["findings"][0]["reasoning"])

    def test_failed_builder_delivery_is_a_deterministic_p1(self) -> None:
        review = failed_delivery_review(
            "failed",
            """### Interaction_Drag

Changed: `Interaction_Drag.swift` · normalized SSIM 0.0194488 · catastrophic sanity **fail**
""",
        )
        self.assertFalse(review["approve"])
        self.assertEqual(review["findings"][0]["severity"], "P1")
        self.assertIn("Interaction_Drag", review["findings"][0]["reasoning"])
        self.assertIn("0.0194488", review["findings"][0]["reasoning"])

    def test_nonfailed_delivery_state_keeps_generic_gate_message(self) -> None:
        review = failed_delivery_review("pending")
        self.assertIn("not proof", review["findings"][0]["reasoning"])

    def test_failed_delivery_attributes_each_pattern_section_accurately(self) -> None:
        review = failed_delivery_review(
            "failed",
            """### First
normalized SSIM 0.95 · catastrophic sanity **pass**

### Second
normalized SSIM 0.12 · catastrophic sanity **fail**

### Third
normalized SSIM unavailable · catastrophic sanity **fail**
""",
        )
        reasoning = review["findings"][0]["reasoning"]
        self.assertNotIn("First mismatches", reasoning)
        self.assertIn("Second mismatches its Origami reference (normalized SSIM 0.12)", reasoning)
        self.assertIn(
            "Third mismatches its Origami reference (normalized SSIM unavailable)", reasoning
        )

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
        self.assertIn("Summary-only because", payload["body"])
