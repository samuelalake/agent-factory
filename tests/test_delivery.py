from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_factory.github_delivery import (
    DELIVERY_END,
    DELIVERY_START,
    delivery_status,
    format_delivery,
    pending_delivery,
    publish,
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
    def test_publish_embeds_native_github_attachments_in_pr_body(self, gh) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            video = Path(directory) / "drag.mp4"
            image.write_bytes(b"png")
            video.write_bytes(b"mp4")
            body = pending_delivery()
            gh.side_effect = [
                json.dumps({"headRefOid": "abc", "body": body}),
                "https://github.com/owner/repo/pull/7\n",
            ]
            publish(
                "owner/repo",
                "7",
                "abc",
                "ready",
                f"![Swami]({image})\n\n![]({video})",
                (image, video),
            )
        args = gh.call_args_list[1].args[0]
        self.assertEqual(args[:6], ["pr", "edit", "7", "--repo", "owner/repo", "--body-file"])
        self.assertEqual(args.count("--attach"), 2)

    @mock.patch("agent_factory.github_delivery._gh")
    def test_wait_returns_failed_delivery_without_sleeping(self, gh) -> None:
        gh.return_value = json.dumps({
            "headRefOid": "abc",
            "body": format_delivery("failed", "Visual sanity failed."),
        })
        status, body = wait_for_delivery(
            "owner/repo", "7", "abc", timeout_seconds=0, poll_seconds=0
        )
        self.assertEqual(status, "failed")
        self.assertIn("Visual sanity failed", body)
