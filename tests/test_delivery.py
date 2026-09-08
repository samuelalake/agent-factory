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
    _publish_attachments,
    replace_delivery,
    wait_for_delivery,
)


class DeliveryTests(unittest.TestCase):
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
                str(image): "https://raw.githubusercontent.com/owner/repo/evidence/swami.png",
                str(video): "https://raw.githubusercontent.com/owner/repo/evidence/drag.mp4",
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
        self.assertIn("raw.githubusercontent.com/owner/repo/evidence/swami.png", payload["body"])
        self.assertIn(
            "[Open interaction recording](https://raw.githubusercontent.com/owner/repo/evidence/drag.mp4)",
            payload["body"],
        )
        self.assertNotIn(str(image), payload["body"])

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
            "https://raw.githubusercontent.com/owner/repo/evidence-commit/pr-7/abc123/01-swami.png",
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
