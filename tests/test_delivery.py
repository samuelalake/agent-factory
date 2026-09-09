from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_factory.github_delivery import (
    DELIVERY_END,
    DELIVERY_START,
    delivery_head,
    delivery_status,
    format_delivery,
    pending_delivery,
    publish,
    _api,
    _publish_attachments,
    _publish_native_attachments,
    replace_delivery,
    wait_for_delivery,
)


class DeliveryTests(unittest.TestCase):
    @mock.patch("agent_factory.github_delivery._gh")
    def test_repository_api_root_has_no_trailing_slash(self, gh) -> None:
        gh.return_value = json.dumps({"default_branch": "main"})

        self.assertEqual(_api("owner/repo", ""), {"default_branch": "main"})

        gh.assert_called_once_with(["api", "repos/owner/repo"], stdin=None)

    def test_pending_section_is_replaced_without_touching_builder_summary(self) -> None:
        body = "Builder summary\n\n" + pending_delivery() + "\n\nExecution details"
        ready = format_delivery("ready", "Current head: `abc1234`\n\nScreenshots here.")
        updated = replace_delivery(body, ready)
        self.assertIn("Builder summary", updated)
        self.assertIn("Execution details", updated)
        self.assertEqual(updated.count(DELIVERY_START), 1)
        self.assertEqual(updated.count(DELIVERY_END), 1)
        self.assertEqual(delivery_status(updated), "ready")

    @mock.patch("agent_factory.github_delivery._gh")
    def test_publish_rejects_stale_head(self, gh) -> None:
        gh.return_value = json.dumps({"headRefOid": "new", "body": pending_delivery()})
        with self.assertRaisesRegex(RuntimeError, "refusing stale"):
            publish("owner/repo", "7", "old", "ready", "Evidence")
        self.assertEqual(gh.call_count, 1)

    @mock.patch("agent_factory.github_delivery._gh")
    def test_publish_detects_head_change_after_body_patch(self, gh) -> None:
        old_head = "a" * 40
        new_head = "b" * 40
        gh.side_effect = [
            json.dumps({"headRefOid": old_head, "body": pending_delivery()}),
            "{}",
            json.dumps({"headRefOid": new_head, "body": pending_delivery()}),
        ]
        with self.assertRaisesRegex(RuntimeError, "raced a new head"):
            publish("owner/repo", "7", old_head, "ready", "Evidence")

    @mock.patch("agent_factory.github_delivery._gh")
    @mock.patch("agent_factory.github_delivery._publish_attachments")
    @mock.patch.dict("os.environ", {"AGENT_FACTORY_MEDIA_UPLOAD_TOKEN": ""})
    def test_publish_embeds_durable_github_evidence_urls(self, upload, gh) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            video = Path(directory) / "drag.mp4"
            image.write_bytes(b"png")
            video.write_bytes(b"mp4")
            body = pending_delivery()
            head = "a" * 40
            gh.side_effect = [
                json.dumps({"headRefOid": head, "body": body}),
                json.dumps({"headRefOid": head, "body": body}),
                "{}",
                json.dumps({
                    "headRefOid": head,
                    "body": format_delivery("ready", "Evidence", head=head),
                }),
            ]
            upload.return_value = {
                str(image): "https://github.com/owner/repo/raw/evidence/swami.png",
                str(video): "https://github.com/owner/repo/raw/evidence/drag.mp4",
            }
            publish(
                "owner/repo",
                "7",
                head,
                "ready",
                f"![Swami]({image})\n\n![]({video})",
                (image, video),
            )
        upload.assert_called_once_with("owner/repo", "7", head, (image, video))
        args = gh.call_args_list[2].args[0]
        self.assertEqual(args[:3], ["api", "repos/owner/repo/pulls/7", "-X"])
        payload = json.loads(gh.call_args_list[2].kwargs["stdin"])
        self.assertIn("github.com/owner/repo/raw/evidence/swami.png", payload["body"])
        self.assertIn(
            "[Open interaction recording](https://github.com/owner/repo/raw/evidence/drag.mp4)",
            payload["body"],
        )
        self.assertNotIn(str(image), payload["body"])

    @mock.patch.dict("os.environ", {"AGENT_FACTORY_MEDIA_UPLOAD_TOKEN": "user-token"})
    @mock.patch("agent_factory.github_delivery._publish_native_attachments")
    @mock.patch("agent_factory.github_delivery._gh")
    def test_publish_embeds_native_media_then_finalizes_as_builder(self, gh, upload) -> None:
        head = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            video = Path(directory) / "drag.mp4"
            image.write_bytes(b"png")
            video.write_bytes(b"mp4")
            original = pending_delivery()
            native_body = format_delivery(
                "ready",
                "![Swami](https://github.com/user-attachments/assets/image)\n\n"
                "https://github.com/user-attachments/assets/video\n\n"
                "![Origami](https://github.com/user-attachments/assets/origami)\n\n"
                "![Diff](https://github.com/user-attachments/assets/diff)",
                head=head,
            )
            gh.side_effect = [
                json.dumps({"headRefOid": head, "body": original}),
                json.dumps({"headRefOid": head, "body": native_body}),
                "{}",
                json.dumps({"headRefOid": head, "body": native_body}),
            ]
            publish(
                "owner/repo",
                "7",
                head,
                "ready",
                f"![Swami]({image})\n\n![]({video})",
                (image, video),
            )
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[:2], ("owner/repo", "7"))
        self.assertEqual(upload.call_args.args[4], "user-token")
        payload = json.loads(gh.call_args_list[2].kwargs["stdin"])
        self.assertEqual(payload["body"], native_body)

    @mock.patch.dict("os.environ", {"AGENT_FACTORY_MEDIA_UPLOAD_TOKEN": "user-token"})
    @mock.patch("agent_factory.github_delivery._publish_native_attachments")
    @mock.patch("agent_factory.github_delivery._gh")
    def test_native_publication_fails_closed_on_non_native_urls(self, gh, upload) -> None:
        head = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "drag.mp4"
            video.write_bytes(b"mp4")
            original = pending_delivery()
            rewritten = format_delivery(
                "ready", "[Open interaction recording](https://example.test/drag.mp4)", head=head
            )
            gh.side_effect = [
                json.dumps({"headRefOid": head, "body": original}),
                json.dumps({"headRefOid": head, "body": rewritten}),
            ]
            with self.assertRaisesRegex(RuntimeError, "native Builder media"):
                publish(
                    "owner/repo", "7", head, "ready", f"![]({video})", (video,)
                )

    @mock.patch("agent_factory.github_delivery._gh")
    def test_native_attachment_upload_uses_separate_token(self, gh) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            image.write_bytes(b"png")
            _publish_native_attachments(
                "owner/repo", "7", f"![Swami]({image})", (image,), "user-token"
            )
        args = gh.call_args.args[0]
        self.assertEqual(args[:6], ["pr", "edit", "7", "--repo", "owner/repo", "--body-file"])
        self.assertIn("--attach", args)
        self.assertEqual(gh.call_args.kwargs["token"], "user-token")

    @mock.patch("agent_factory.github_delivery._api")
    def test_attachment_commit_is_current_head_keyed_and_atomic(self, api) -> None:
        api.side_effect = [
            {"sha": "blob-sha"},
            {"object": {"sha": "parent-sha"}},
            {"tree": {"sha": "base-tree"}},
            {"sha": "new-tree"},
            {"sha": "evidence-commit"},
            {},
        ]
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            image.write_bytes(b"png")
            urls = _publish_attachments("owner/repo", "7", "abc123", (image,))
        self.assertEqual(
            urls[str(image)],
            "https://github.com/owner/repo/raw/evidence-commit/pr-7/abc123/01-swami.png",
        )
        tree_payload = api.call_args_list[3].kwargs["payload"]
        self.assertEqual(tree_payload["base_tree"], "base-tree")
        self.assertEqual(tree_payload["tree"][0]["path"], "pr-7/abc123/01-swami.png")
        ref_payload = api.call_args_list[5].kwargs["payload"]
        self.assertEqual(ref_payload, {"sha": "evidence-commit", "force": False})

    @mock.patch("agent_factory.github_delivery._gh")
    def test_wait_returns_failed_delivery_without_sleeping(self, gh) -> None:
        head = "a" * 40
        gh.return_value = json.dumps({
            "headRefOid": head,
            "body": format_delivery("failed", "Visual sanity failed.", head=head),
        })
        status, body = wait_for_delivery(
            "owner/repo", "7", head, timeout_seconds=0, poll_seconds=0
        )
        self.assertEqual(status, "failed")
        self.assertIn("Visual sanity failed", body)

    @mock.patch("agent_factory.github_delivery._gh")
    def test_wait_rejects_ready_delivery_for_another_head(self, gh) -> None:
        old_head = "a" * 40
        new_head = "b" * 40
        body = format_delivery("ready", "Old evidence", head=old_head)
        gh.return_value = json.dumps({"headRefOid": new_head, "body": body})
        status, _ = wait_for_delivery(
            "owner/repo", "7", new_head, timeout_seconds=0, poll_seconds=0
        )
        self.assertEqual(status, "stale")
        self.assertEqual(delivery_head(body), old_head)
