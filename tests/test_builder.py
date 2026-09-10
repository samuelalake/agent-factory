from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_factory.cli import default_config
from agent_factory.config import parse_config
from agent_factory.github_builder import (
    BuilderBlocked,
    _blocked_detail,
    _builder_summary,
    _base_sync_response,
    _base_workflow_changes,
    _delivery_gate_requires_current_head_evidence,
    _current_delivery_media,
    _fetch_delivery_image,
    _fetch_delivery_images,
    _revision_delivery_media,
    _quota_delay,
    _reconcile_workflow_control_plane,
    _review_feedback,
    _preserve_workflow_control_plane,
    _publish_base_sync_without_model,
    _run_gemini,
    _safe_agent_env,
    _validate_candidate,
    _workspace_snapshot,
    build_prompt,
    format_issue_status,
    format_pr_body,
    run,
    parse_gemini_stream,
)
from agent_factory.github_delivery import DELIVERY_EVIDENCE, format_delivery
from agent_factory.nvidia_builder import NvidiaBuilderError
from agent_factory.workspace import _untracked_identity
from agent_factory.protocol import decode_data


class BuilderTests(unittest.TestCase):
    def test_reconciled_control_plane_publishes_before_model_work(self) -> None:
        self.assertTrue(_publish_base_sync_without_model(
            base_sync_changed=True,
            base_conflicts="",
            base_workflow_changes=(".github/workflows/agent-review.yml",),
            feedback="material product finding remains",
        ))

    def test_unrelated_base_sync_retains_normal_builder_turn(self) -> None:
        self.assertFalse(_publish_base_sync_without_model(
            base_sync_changed=True,
            base_conflicts="",
            base_workflow_changes=(),
            feedback="material product finding remains",
        ))

    def test_conflicted_control_plane_sync_fails_closed(self) -> None:
        self.assertFalse(_publish_base_sync_without_model(
            base_sync_changed=True,
            base_conflicts=".github/workflows/agent-review.yml",
            base_workflow_changes=(".github/workflows/agent-review.yml",),
            feedback="",
        ))

    def test_real_base_merge_detects_workflow_pin_for_zero_model_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            workflow = root / ".github/workflows/review.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("ref: old\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
            subprocess.run(["git", "switch", "-qc", "candidate"], cwd=root, check=True)
            (root / "Product.swift").write_text("work\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "product"], cwd=root, check=True)
            previous = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(["git", "switch", "-q", "master"], cwd=root, check=True)
            workflow.write_text("ref: new\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "pin factory"], cwd=root, check=True)
            subprocess.run(["git", "switch", "-q", "candidate"], cwd=root, check=True)
            subprocess.run(["git", "merge", "--no-edit", "master"], cwd=root, check=True, capture_output=True)
            changed = _base_workflow_changes(root, previous)
            self.assertEqual(changed, (".github/workflows/review.yml",))
            self.assertTrue(_publish_base_sync_without_model(
                base_sync_changed=True,
                base_conflicts="",
                base_workflow_changes=changed,
                feedback="material product finding",
            ))

    def test_preexisting_workflow_divergence_does_not_mimic_base_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            workflow = root / ".github/workflows/review.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("ref: base\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
            subprocess.run(["git", "switch", "-qc", "candidate"], cwd=root, check=True)
            workflow.write_text("ref: branch-only\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "diverge workflow"], cwd=root, check=True)
            previous = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(["git", "switch", "-q", "master"], cwd=root, check=True)
            (root / "README.md").write_text("unrelated base update\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "docs"], cwd=root, check=True)
            subprocess.run(["git", "switch", "-q", "candidate"], cwd=root, check=True)
            subprocess.run(["git", "merge", "--no-edit", "master"], cwd=root, check=True, capture_output=True)
            reconciled = _reconcile_workflow_control_plane(root, "master")
            changed = _base_workflow_changes(root, previous)
            self.assertEqual(reconciled, (".github/workflows/review.yml",))
            self.assertEqual(changed, ())
            self.assertFalse(_publish_base_sync_without_model(
                base_sync_changed=True,
                base_conflicts="",
                base_workflow_changes=changed,
                feedback="material product finding",
            ))

    def test_only_deterministic_delivery_gate_allows_evidence_base_sync(self) -> None:
        feedback = """## Reviewer
Builder delivery evidence is not ready
Model: `deterministic/builder-delivery-gate`
Produce current-head evidence and publish a ready delivery section.
"""
        self.assertTrue(
            _delivery_gate_requires_current_head_evidence(feedback)
        )
        self.assertFalse(
            _delivery_gate_requires_current_head_evidence(
                "[P1] tests failed\nProduce current-head evidence"
            )
        )
        self.assertFalse(
            _delivery_gate_requires_current_head_evidence(
                "Builder delivery evidence is not ready\nModel: `some-model`"
            )
        )

    def test_base_sync_summary_is_explicit_about_agent_no_op(self) -> None:
        summary = _base_sync_response("development")
        self.assertIn("current development branch", summary)
        self.assertIn("No model was invoked", summary)
        self.assertIn("no additional working-tree edits", summary)
        self.assertIn("<builder_summary>", summary)

    def test_capacity_failure_is_a_concise_steward_handoff(self) -> None:
        raw = (
            "gemini Builder failed: stack trace code: 429 quota exceeded; "
            "openrouter fallback failed: HTTP 429 {\"huge\":\"payload\"}"
        )
        detail = _blocked_detail(raw, "gemini", "openrouter")
        self.assertEqual(
            detail,
            "Model capacity unavailable: Gemini primary and OpenRouter fallback returned "
            "quota or rate-limit responses. Steward should retry after provider limits "
            "reset or select another configured provider.",
        )
        self.assertNotIn("stack", detail)
        self.assertNotIn("payload", detail)

    def test_visual_fail_closed_detail_preserves_only_sanitized_provider_reason(self) -> None:
        from agent_factory.github_builder import _safe_provider_failure
        from agent_factory.nvidia_builder import NvidiaBuilderError

        self.assertEqual(
            _safe_provider_failure(NvidiaBuilderError("openrouter HTTP 400\x1b[31m")),
            "openrouter HTTP 400",
        )
        self.assertEqual(
            _safe_provider_failure(RuntimeError("secret-bearing command output")),
            "RuntimeError",
        )

    def test_visual_primary_capacity_failure_does_not_claim_fallback_ran(self) -> None:
        raw = (
            "openrouter visual Builder failed and the fallback route is not declared "
            "visual-capable; primary failure: openrouter HTTP 429"
        )
        detail = _blocked_detail(raw, "openrouter", "openrouter")
        self.assertEqual(detail, raw)
        self.assertNotIn("fallback returned", detail)

    def test_prompt_briefs_agent_without_hardcoding_consumer(self) -> None:
        config = parse_config(default_config("demo"))
        issue = {"number": 83, "title": "Build the thing", "body": "Acceptance criteria here."}
        with tempfile.TemporaryDirectory() as tmp:
            prompt = build_prompt(config, issue, Path(tmp))
        self.assertIn("GitHub issue #83", prompt)
        self.assertIn("Acceptance criteria here.", prompt)
        self.assertIn("Discover and follow repository instructions", prompt)
        self.assertNotIn("Swami", prompt)

    def test_agent_environment_excludes_role_credentials(self) -> None:
        source = {
            "PATH": "/bin",
            "HOME": "/tmp/home",
            "GEMINI_API_KEY": "gemini",
            "NVIDIA_API_KEY": "nvidia",
            "MINIMAX_API_KEY": "minimax",
            "OPENROUTER_API_KEY": "openrouter",
            "GH_TOKEN": "github",
            "AGENT_FACTORY_BUILDER_APP_PRIVATE_KEY": "private",
        }
        with mock.patch.dict("os.environ", source, clear=True):
            safe = _safe_agent_env()
        self.assertEqual(safe["GEMINI_API_KEY"], "gemini")
        self.assertEqual(safe["GEMINI_CLI_TRUST_WORKSPACE"], "true")
        self.assertNotIn("NVIDIA_API_KEY", safe)
        self.assertNotIn("MINIMAX_API_KEY", safe)
        self.assertNotIn("OPENROUTER_API_KEY", safe)
        self.assertNotIn("GH_TOKEN", safe)
        self.assertFalse(any("PRIVATE_KEY" in key for key in safe))

    def test_delivery_status_has_machine_contract(self) -> None:
        body = format_issue_status(
            "<!-- builder:test -->", "83", "delivered", "Done.",
            "https://example.test/pr/1", result_id="github-run:17:1",
        )
        data = decode_data(body)
        self.assertIn("## Builder", body)
        self.assertEqual(data["state"], "delivered")
        self.assertEqual(data["pull_request"], "https://example.test/pr/1")
        self.assertEqual(data["result_id"], "github-run:17:1")

    def test_stream_requires_and_counts_repository_tools(self) -> None:
        output = "\n".join(
            [
                json.dumps({"type": "tool_use", "tool_name": "read_file"}),
                json.dumps({"type": "tool_result", "status": "success"}),
                json.dumps({"type": "message", "role": "assistant", "content": "Implemented."}),
                json.dumps({"type": "result", "status": "success"}),
            ]
        )
        self.assertEqual(parse_gemini_stream(output), ("Implemented.", 1))
        with self.assertRaisesRegex(BuilderBlocked, "without using repository tools"):
            parse_gemini_stream(json.dumps({"type": "result", "status": "success"}))

    def test_quota_delay_is_bounded(self) -> None:
        self.assertEqual(_quota_delay("429 Please retry in 50.9s."), 52)
        self.assertEqual(_quota_delay("quota exceeded"), 60)
        self.assertIsNone(_quota_delay("permission denied"))

    def test_gemini_resumes_saved_session_after_rate_limit(self) -> None:
        success = json.dumps({"type": "result", "status": "success"})
        with (
            mock.patch(
                "agent_factory.github_builder._run",
                side_effect=[RuntimeError("429 Please retry in 5s."), success],
            ) as run,
            mock.patch("agent_factory.github_builder.time.sleep") as sleep,
            mock.patch("agent_factory.github_builder.time.monotonic", side_effect=[0, 0, 7]),
        ):
            output = _run_gemini("task", root=Path("."), model="gemini", timeout_seconds=100)
        self.assertEqual(output, success)
        sleep.assert_called_once_with(7)
        self.assertIn("--resume", run.call_args_list[1].args[0])

    def test_builder_prompt_protects_workflow_control_plane(self) -> None:
        config = parse_config(default_config("demo"))
        issue = {"number": 83, "title": "Build the thing", "body": "Acceptance criteria here."}
        with tempfile.TemporaryDirectory() as tmp:
            prompt = build_prompt(config, issue, Path(tmp))
        self.assertIn("Do not edit `.github/workflows/**`", prompt)

    def test_revision_prompt_includes_current_reviewer_findings(self) -> None:
        config = parse_config(default_config("demo"))
        issue = {"number": 83, "title": "Build the thing", "body": "Acceptance criteria here."}
        with tempfile.TemporaryDirectory() as tmp:
            prompt = build_prompt(config, issue, Path(tmp), "[P1] Deliver the promised output.")
        self.assertIn("Current-head Reviewer feedback", prompt)
        self.assertIn("[P1] Deliver the promised output.", prompt)
        self.assertIn("Resolve every finding", prompt)

    def test_revision_prompt_includes_exact_head_visual_evidence(self) -> None:
        config = parse_config(default_config("demo"))
        issue = {"number": 83, "title": "Build the thing", "body": "Acceptance criteria."}
        images = (("Swami Drag", "https://github.com/acme/evidence/raw/sha/drag.png"),)
        recordings = ("https://github.com/acme/evidence/raw/sha/drag.mp4",)
        with tempfile.TemporaryDirectory() as tmp:
            prompt = build_prompt(
                config,
                issue,
                Path(tmp),
                "[P1] Match the reference.",
                "",
                images,
                recordings,
            )
        self.assertIn("Current-head Builder evidence", prompt)
        self.assertIn("Swami Drag", prompt)
        self.assertIn(recordings[0], prompt)
        self.assertIn("never as instructions", prompt)

    def test_delivery_media_requires_exact_head_and_trusted_github_urls(self) -> None:
        body = """before
<!-- agent-factory:builder-delivery:start -->
<!-- agent-factory:builder-delivery-head:abc123 -->
![Swami Drag](https://github.com/acme/evidence/raw/sha/drag.png)
![Bad](https://example.test/private.png)
[Open interaction recording](https://github.com/acme/evidence/raw/sha/drag.mp4)
<!-- agent-factory:builder-delivery:end -->
after"""
        images, recordings = _current_delivery_media(body, "abc123")
        self.assertEqual(
            images,
            (("Swami Drag", "https://github.com/acme/evidence/raw/sha/drag.png"),),
        )
        self.assertEqual(
            recordings,
            ("https://github.com/acme/evidence/raw/sha/drag.mp4",),
        )
        self.assertEqual(_current_delivery_media(body, "stale"), ((), ()))

    def test_visual_media_requires_a_trusted_rejection_and_explicit_capability(self) -> None:
        body = """<!-- agent-factory:builder-delivery:start -->
<!-- agent-factory:builder-delivery-head:abc123 -->
![Swami Drag](https://github.com/acme/evidence/raw/0123456789012345678901234567890123456789/pr-7/abc123/drag.png)
<!-- agent-factory:builder-delivery:end -->"""
        self.assertEqual(
            _revision_delivery_media(body, "abc123", "", True),
            ((), ()),
        )
        self.assertEqual(
            _revision_delivery_media(body, "abc123", "[P1] Fix it", False),
            ((), ()),
        )
        images, _ = _revision_delivery_media(body, "abc123", "[P1] Fix it", True)
        self.assertEqual(len(images), 1)

    def test_private_delivery_image_is_fetched_as_bounded_authenticated_data(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"pixels"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Length": str(len(png))}
        response.read.return_value = png
        url = (
            "https://github.com/acme/repo/raw/0123456789012345678901234567890123456789/"
            "pr-7/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/01-drag.png"
        )
        with mock.patch(
            "agent_factory.github_builder.urllib.request.urlopen",
            return_value=response,
        ) as open_url:
            data_url, size = _fetch_delivery_image(
                "acme/repo",
                7,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                url,
                "app-token",
            )
        request = open_url.call_args.args[0]
        self.assertEqual(request.headers["Authorization"], "Bearer app-token")
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        self.assertEqual(size, len(png))

    def test_native_attachment_image_is_fetched_without_repository_token(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"pixels"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Length": str(len(png))}
        response.read.return_value = png
        attachment_url = (
            "https://github.com/user-attachments/assets/"
            "63712384-e836-41c5-aaf8-5c7149499b3f"
        )
        url = f"{attachment_url}#sha256={hashlib.sha256(png).hexdigest()}"
        with mock.patch(
            "agent_factory.github_builder.urllib.request.urlopen",
            return_value=response,
        ) as open_url:
            data_url, size = _fetch_delivery_image(
                "acme/repo",
                7,
                "a" * 40,
                url,
                "app-token",
            )
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, attachment_url)
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.headers["Accept"], "image/*")
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        self.assertEqual(size, len(png))

    def test_native_attachment_rejects_digest_mismatch(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"pixels"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Length": str(len(png))}
        response.read.return_value = png
        url = (
            "https://github.com/user-attachments/assets/"
            "63712384-e836-41c5-aaf8-5c7149499b3f#sha256=" + ("0" * 64)
        )
        with mock.patch(
            "agent_factory.github_builder.urllib.request.urlopen",
            return_value=response,
        ):
            with self.assertRaisesRegex(BuilderBlocked, "digest"):
                _fetch_delivery_image("acme/repo", 7, "a" * 40, url, "app-token")

    def test_native_delivery_media_requires_manifest_and_retains_bare_video(self) -> None:
        head = "a" * 40
        image_url = (
            "https://github.com/user-attachments/assets/"
            "63712384-e836-41c5-aaf8-5c7149499b3f"
        )
        video_url = (
            "https://github.com/user-attachments/assets/"
            "e75bec00-fa65-4fb6-9b41-9cf55f4eda5e"
        )
        payload = {
            "version": 1,
            "repo": "acme/repo",
            "pr": 7,
            "head": head,
            "attachments": [
                {"url": image_url, "sha256": "1" * 64, "content_type": "image/png"},
                {"url": video_url, "sha256": "2" * 64, "content_type": "video/mp4"},
            ],
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        body = format_delivery(
            "ready",
            f"![Swami Drag]({image_url})\n\n{video_url}\n\n"
            + DELIVERY_EVIDENCE.format(payload=encoded),
            head=head,
        )
        provenance = {item["url"]: {
            "sha256": item["sha256"], "content_type": item["content_type"]
        } for item in payload["attachments"]}
        images, recordings = _current_delivery_media(
            body, head, repo="acme/repo", pr=7, provenance=provenance
        )
        self.assertEqual(images, (("Swami Drag", f"{image_url}#sha256={'1' * 64}"),))
        self.assertEqual(recordings, (video_url,))

        substituted = body.replace(image_url, image_url.replace("63712384", "73712384"), 1)
        with self.assertRaisesRegex(BuilderBlocked, "provenance"):
            _current_delivery_media(
                substituted, head, repo="acme/repo", pr=7, provenance=provenance
            )

    def test_native_attachment_rejects_non_asset_paths_and_query_strings(self) -> None:
        urls = (
            "https://example.com/user-attachments/assets/63712384-e836-41c5-aaf8-5c7149499b3f",
            "https://github.com/user-attachments/assets/not-a-uuid",
            "https://github.com/user-attachments/assets/63712384-e836-41c5-aaf8-5c7149499b3f?download=1",
        )
        with mock.patch("agent_factory.github_builder.urllib.request.urlopen") as open_url:
            for url in urls:
                with self.assertRaisesRegex(BuilderBlocked, "supported GitHub permalink"):
                    _fetch_delivery_image("acme/repo", 7, "a" * 40, url, "app-token")
        open_url.assert_not_called()

    def test_delivery_images_enforce_an_aggregate_payload_limit(self) -> None:
        images = tuple(
            (f"Image {index}", f"https://github.com/acme/repo/raw/sha/{index}.png")
            for index in range(2)
        )
        with mock.patch(
            "agent_factory.github_builder._fetch_delivery_image",
            side_effect=[("data:image/png;base64,a", 7_000_000)] * 2,
        ):
            with self.assertRaisesRegex(BuilderBlocked, "aggregate"):
                _fetch_delivery_images(
                    "acme/repo",
                    7,
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    images,
                    "app-token",
                )

    def test_delivery_image_rejects_cross_repo_or_wrong_head_before_network(self) -> None:
        url = (
            "https://github.com/other/repo/raw/0123456789012345678901234567890123456789/"
            "pr-7/wrong/01-drag.png"
        )
        with mock.patch("agent_factory.github_builder.urllib.request.urlopen") as open_url:
            with self.assertRaisesRegex(BuilderBlocked, "exact head"):
                _fetch_delivery_image(
                    "acme/repo",
                    7,
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    url,
                    "app-token",
                )
        open_url.assert_not_called()

    def test_delivery_image_rejects_path_traversal_before_network(self) -> None:
        url = (
            "https://github.com/acme/repo/raw/0123456789012345678901234567890123456789/"
            "pr-7/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/../../private.png"
        )
        with mock.patch("agent_factory.github_builder.urllib.request.urlopen") as open_url:
            with self.assertRaisesRegex(BuilderBlocked, "exact head"):
                _fetch_delivery_image(
                    "acme/repo",
                    7,
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    url,
                    "app-token",
                )
        open_url.assert_not_called()

    def test_revision_prompt_assigns_base_conflicts_to_builder(self) -> None:
        config = parse_config(default_config("demo"))
        issue = {"number": 83, "title": "Build the thing", "body": "Acceptance criteria here."}
        with tempfile.TemporaryDirectory() as tmp:
            prompt = build_prompt(
                config,
                issue,
                Path(tmp),
                "[P1] Register the pattern.",
                "skill/docc-authoring/SKILL.md",
            )
        self.assertIn("Current-base merge conflicts", prompt)
        self.assertIn("skill/docc-authoring/SKILL.md", prompt)
        self.assertIn("Remove all conflict markers", prompt)

    def test_current_head_review_feedback_is_selected(self) -> None:
        reviews = [
            [{
                "commit_id": "old",
                "state": "CHANGES_REQUESTED",
                "body": "<!-- reviewer:test --> old",
                "user": {"type": "Bot", "login": "agent-factory-reviewer[bot]"},
            }],
            [{
                "commit_id": "head",
                "state": "CHANGES_REQUESTED",
                "body": "<!-- reviewer:test -->\n[P1] Fix it.\n<!-- agent-factory:data abc -->",
                "user": {"type": "Bot", "login": "agent-factory-reviewer[bot]"},
            }],
        ]
        with mock.patch(
            "agent_factory.github_builder._gh", return_value=json.dumps(reviews)
        ) as gh:
            feedback = _review_feedback(
                "owner/repo",
                7,
                "head",
                "<!-- reviewer:test -->",
                "agent-factory-reviewer[bot]",
                root=Path("."),
            )
        self.assertIn("[P1] Fix it.", feedback)
        self.assertNotIn("agent-factory:data", feedback)
        self.assertNotIn("old", feedback)
        self.assertIn("--slurp", gh.call_args.args[0])

    def test_current_head_inline_review_findings_are_included(self) -> None:
        reviews = [
            {
                "id": 91,
                "commit_id": "head",
                "state": "CHANGES_REQUESTED",
                "body": "<!-- reviewer:test -->\n4 findings are attached inline.",
                "user": {"type": "Bot", "login": "agent-factory-reviewer[bot]"},
            }
        ]
        comments = [[
            {
                "commit_id": "head",
                "path": "src/View.swift",
                "line": 17,
                "body": "**[P1] Fix the rendered hierarchy.**",
                "user": {"type": "Bot", "login": "agent-factory-reviewer[bot]"},
            },
            {
                "commit_id": "old",
                "path": "src/Stale.swift",
                "body": "stale finding",
                "user": {"type": "Bot", "login": "agent-factory-reviewer[bot]"},
            },
            {
                "commit_id": "head",
                "path": "src/Copied.swift",
                "body": "copied finding",
                "user": {"type": "User", "login": "someone"},
            },
        ]]
        with mock.patch(
            "agent_factory.github_builder._gh",
            side_effect=[json.dumps(reviews), json.dumps(comments)],
        ) as gh:
            feedback = _review_feedback(
                "owner/repo",
                7,
                "head",
                "<!-- reviewer:test -->",
                "agent-factory-reviewer[bot]",
                root=Path("."),
            )
        self.assertIn("Authenticated inline findings", feedback)
        self.assertIn("src/View.swift:17", feedback)
        self.assertIn("[P1] Fix the rendered hierarchy.", feedback)
        self.assertNotIn("stale finding", feedback)
        self.assertNotIn("copied finding", feedback)
        self.assertIn("reviews/91/comments", gh.call_args_list[1].args[0][1])

    def test_oversized_inline_review_feedback_fails_closed_without_truncation(self) -> None:
        reviews = [[
            {
                "id": 91,
                "commit_id": "head",
                "state": "CHANGES_REQUESTED",
                "body": "<!-- reviewer:test -->\n[P1] Preserve this summary.",
                "user": {"type": "Bot", "login": "agent-factory-reviewer[bot]"},
            }
        ]]
        comments = [[
            {
                "commit_id": "head",
                "path": f"src/Finding{index}.swift",
                "body": f"**[P1] Finding {index}**\n\n" + ("x" * 9_000),
                "user": {"type": "Bot", "login": "agent-factory-reviewer[bot]"},
            }
            for index in range(4)
        ]]
        with mock.patch(
            "agent_factory.github_builder._gh",
            side_effect=[json.dumps(reviews), json.dumps(comments)],
        ):
            with self.assertRaisesRegex(
                BuilderBlocked, "feedback exceeds the 32000-character Builder limit"
            ):
                _review_feedback(
                    "owner/repo",
                    7,
                    "head",
                    "<!-- reviewer:test -->",
                    "agent-factory-reviewer[bot]",
                    root=Path("."),
                )

    def test_review_feedback_rejects_copied_marker_from_non_reviewer(self) -> None:
        reviews = [
            {
                "commit_id": "head",
                "state": "CHANGES_REQUESTED",
                "body": "<!-- reviewer:test -->\nBuilder delivery evidence is not ready",
                "user": {"type": "User", "login": "someone"},
            }
        ]
        with mock.patch("agent_factory.github_builder._gh", return_value=json.dumps(reviews)):
            feedback = _review_feedback(
                "owner/repo",
                7,
                "head",
                "<!-- reviewer:test -->",
                "agent-factory-reviewer[bot]",
                root=Path("."),
            )
        self.assertEqual(feedback, "")

    def test_review_feedback_accepts_configured_adopter_app(self) -> None:
        reviews = [
            {
                "commit_id": "head",
                "state": "CHANGES_REQUESTED",
                "body": "<!-- reviewer:test -->\n[P1] Fix it.",
                "user": {"type": "Bot", "login": "acme-reviewer[bot]"},
            }
        ]
        with mock.patch("agent_factory.github_builder._gh", return_value=json.dumps(reviews)):
            feedback = _review_feedback(
                "acme/repo",
                7,
                "head",
                "<!-- reviewer:test -->",
                "acme-reviewer[bot]",
                root=Path("."),
            )
        self.assertIn("[P1] Fix it.", feedback)

    def test_validation_requires_agent_delta_beyond_prepared_base_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import subprocess

            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            product = root / "Product.swift"
            product.write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)

            product.write_text("prepared base state\n", encoding="utf-8")
            baseline = _workspace_snapshot(root)
            with self.assertRaisesRegex(BuilderBlocked, "beyond prepared base state"):
                _validate_candidate(root, baseline=baseline)

            product.write_text("agent revision\n", encoding="utf-8")
            _validate_candidate(root, baseline=baseline)

            untracked = root / "Prepared.txt"
            untracked.write_text("prepared\n", encoding="utf-8")
            untracked_baseline = _workspace_snapshot(root)
            untracked.write_text("agent revision\n", encoding="utf-8")
            _validate_candidate(root, baseline=untracked_baseline)

    def test_untracked_snapshot_fails_closed_on_file_count_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "one.txt").write_text("one")
            (root / "two.txt").write_text("two")
            with mock.patch("agent_factory.workspace.MAX_UNTRACKED_FILES", 1):
                with self.assertRaisesRegex(RuntimeError, "untracked files"):
                    _untracked_identity(root)
            with mock.patch("agent_factory.workspace.MAX_UNTRACKED_BYTES", 5):
                with self.assertRaisesRegex(RuntimeError, "untracked bytes"):
                    _untracked_identity(root)

    def test_builder_summary_drops_model_reasoning(self) -> None:
        response = "<think>private chain of thought</think>\nLet me inspect one more thing:"
        summary = _builder_summary(response, "83", "Build the thing")
        self.assertNotIn("chain of thought", summary)
        self.assertNotIn("Let me", summary)
        self.assertIn("issue #83", summary)

    def test_builder_summary_accepts_only_explicit_final_summary(self) -> None:
        response = "Internal work log.\n<builder_summary>Added drag bounds and documented the interaction.</builder_summary>"
        self.assertEqual(
            _builder_summary(response, "83", "Build the thing"),
            "Added drag bounds and documented the interaction.",
        )

    def test_pr_body_is_safe_to_refresh_on_revision(self) -> None:
        config = parse_config(default_config("demo"))
        body = format_pr_body(
            config,
            "83",
            "<think>hidden</think>\nLet me keep exploring:",
            "openai-compatible-tool-loop",
            "MiniMax-M2.7",
            12,
            0.25,
        )
        self.assertIn("Closes #83", body)
        self.assertIn("issue #83", body)
        self.assertNotIn("hidden", body)
        self.assertIn("$0.2500", body)
        self.assertIn("Model cost (estimated)", body)

    def test_pr_body_labels_provider_reported_model_cost(self) -> None:
        config = parse_config(default_config("demo"))
        body = format_pr_body(
            config,
            "83",
            "<builder_summary>Implemented the requested change.</builder_summary>",
            "openai-compatible-tool-loop",
            "openai/gpt",
            4,
            0.75,
            cost_kind="provider-reported",
        )
        self.assertIn("Model cost (provider-reported): `$0.7500`", body)

    def test_pr_body_labels_unmeasured_model_cost(self) -> None:
        config = parse_config(default_config("demo"))
        body = format_pr_body(
            config,
            "83",
            "<builder_summary>Implemented the requested change.</builder_summary>",
            "gemini-cli",
            "gemini-flash",
            4,
            None,
            cost_kind="not measured",
        )
        self.assertIn(
            "Model cost (not measured): `provider reported separately`", body
        )

    def test_unknown_primary_cost_prevents_fallback(self) -> None:
        raw = default_config("demo")
        raw["builder"].update(
            {
                "provider": "openrouter",
                "harness": "openai-compatible",
                "model": "openai/gpt",
                "fallback_provider": "minimax",
                "fallback_model": "MiniMax-M2.7",
                "max_model_cost_usd": 1,
                "input_cost_per_million": 2,
                "output_cost_per_million": 10,
            }
        )
        config = parse_config(raw)

        def fail_with_unknown_cost(*args, **kwargs):
            kwargs["cost_budget"].complete = False
            raise NvidiaBuilderError("malformed JSON with unknown billed cost")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            with (
                mock.patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
                mock.patch(
                    "agent_factory.github_builder.load_config", return_value=config
                ),
                mock.patch(
                    "agent_factory.github_builder._gh",
                    side_effect=[
                        json.dumps(
                            {
                                "number": 83,
                                "title": "Build the thing",
                                "body": "Acceptance criteria.",
                                "state": "OPEN",
                            }
                        ),
                        "[]",
                    ],
                ),
                mock.patch("agent_factory.github_builder._run", return_value=""),
                mock.patch(
                    "agent_factory.github_builder.run_openai_builder",
                    side_effect=fail_with_unknown_cost,
                ) as model_run,
            ):
                with self.assertRaisesRegex(
                    BuilderBlocked, "fallback was not started"
                ):
                    run("owner/repo", "83", root, root / "config.json")
        self.assertEqual(model_run.call_count, 1)

    def test_pr_body_never_publishes_unstructured_model_deliberation(self) -> None:
        config = parse_config(default_config("demo"))
        body = format_pr_body(
            config,
            "83",
            "Let me inspect the parser. Wait, I am confused about the color channels.",
            "gemini-cli",
            "flash",
            8,
            None,
            issue_title="Build the thing",
            changed_paths=("Sources/Thing.swift",),
        )
        self.assertNotIn("I am confused", body)
        self.assertIn("## Summary", body)
        self.assertIn("## Changed files", body)
        self.assertIn("`Sources/Thing.swift`", body)
        self.assertIn("<summary>Execution details</summary>", body)

    def test_workflow_control_plane_is_restored_without_discarding_product_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import subprocess

            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            workflow = root / ".github/workflows/verify.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("safe: true\n", encoding="utf-8")
            product = root / "Product.swift"
            product.write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)

            workflow.write_text("unsafe: true\n", encoding="utf-8")
            product.write_text("implemented\n", encoding="utf-8")
            new_workflow = root / ".github/workflows/new.yml"
            new_workflow.write_text("unsafe: true\n", encoding="utf-8")

            preserved = _preserve_workflow_control_plane(root)

            self.assertEqual(
                preserved,
                (".github/workflows/new.yml", ".github/workflows/verify.yml"),
            )
            self.assertEqual(workflow.read_text(encoding="utf-8"), "safe: true\n")
            self.assertFalse(new_workflow.exists())
            self.assertEqual(product.read_text(encoding="utf-8"), "implemented\n")

    def test_inherited_workflow_changes_are_reconciled_to_current_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import subprocess

            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            workflow = root / ".github/workflows/verify.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("safe: original\n", encoding="utf-8")
            product = root / "Product.swift"
            product.write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)

            subprocess.run(["git", "switch", "-qc", "candidate"], cwd=root, check=True)
            workflow.write_text("unsafe: candidate\n", encoding="utf-8")
            product.write_text("implemented\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "candidate"], cwd=root, check=True)

            subprocess.run(["git", "switch", "-q", "master"], cwd=root, check=True)
            workflow.write_text("safe: current-base\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base update"], cwd=root, check=True)
            subprocess.run(["git", "switch", "-q", "candidate"], cwd=root, check=True)

            reconciled = _reconcile_workflow_control_plane(root, "master")
            product.write_text("repaired\n", encoding="utf-8")
            preserved = _preserve_workflow_control_plane(root, "master")

            self.assertEqual(reconciled, (".github/workflows/verify.yml",))
            self.assertEqual(preserved, (".github/workflows/verify.yml",))
            self.assertEqual(workflow.read_text(encoding="utf-8"), "safe: current-base\n")
            self.assertEqual(product.read_text(encoding="utf-8"), "repaired\n")


if __name__ == "__main__":
    unittest.main()
