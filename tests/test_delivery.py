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
    _rewrite_attachment_references,
    _stage_native_attachments,
    _validate_attachments,
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

    @mock.patch.dict("os.environ", {"AGENT_FACTORY_MEDIA_UPLOAD_TOKEN": "media-token"})
    @mock.patch("agent_factory.github_delivery._stage_native_attachments")
    @mock.patch("agent_factory.github_delivery._gh")
    def test_publish_stages_media_then_refreshes_before_builder_patch(
        self, gh, stage
    ) -> None:
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
                "https://github.com/user-attachments/assets/video",
                head=head,
            )
            gh.side_effect = [
                json.dumps({"headRefOid": head, "body": original}),
                json.dumps({"headRefOid": head, "body": original}),
                "{}",
                json.dumps({"headRefOid": head, "body": native_body}),
            ]
            stage.return_value = {
                str(image): "https://github.com/user-attachments/assets/image",
                str(video): "https://github.com/user-attachments/assets/video",
            }
            publish(
                "owner/repo",
                "7",
                head,
                "ready",
                f"![Swami]({image})\n\n![]({video})",
                (image, video),
            )
        stage.assert_called_once_with("owner/repo", "7", (image, video), "media-token")
        patch_call = gh.call_args_list[2]
        self.assertEqual(
            patch_call.args[0][:3], ["api", "repos/owner/repo/pulls/7", "-X"]
        )
        payload = json.loads(patch_call.kwargs["stdin"])
        self.assertIn("![Swami](https://github.com/user-attachments/assets/image)", payload["body"])
        self.assertIn("\nhttps://github.com/user-attachments/assets/video\n", payload["body"])

    @mock.patch.dict("os.environ", {"AGENT_FACTORY_MEDIA_UPLOAD_TOKEN": "media-token"})
    @mock.patch("agent_factory.github_delivery._stage_native_attachments")
    @mock.patch("agent_factory.github_delivery._gh")
    def test_native_media_detects_revision_before_builder_patch(self, gh, stage) -> None:
        old_head = "a" * 40
        new_head = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            image.write_bytes(b"png")
            original = pending_delivery()
            newer = "New Builder summary\n\n" + pending_delivery()
            gh.side_effect = [
                json.dumps({"headRefOid": old_head, "body": original}),
                json.dumps({"headRefOid": new_head, "body": newer}),
            ]
            stage.return_value = {
                str(image): "https://github.com/user-attachments/assets/image"
            }
            with self.assertRaisesRegex(RuntimeError, "refusing stale"):
                publish(
                    "owner/repo", "7", old_head, "ready", f"![Swami]({image})", (image,)
                )
        self.assertEqual(gh.call_count, 2)
        self.assertFalse(any("PATCH" in call.args[0] for call in gh.call_args_list))

    @mock.patch("agent_factory.github_delivery._gh")
    @mock.patch("agent_factory.github_delivery.uuid.uuid4")
    def test_staging_comment_is_deleted_after_extracting_native_urls(
        self, unique_id, gh
    ) -> None:
        unique_id.return_value.hex = "fixed"
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            video = Path(directory) / "drag.mp4"
            image.write_bytes(b"png")
            video.write_bytes(b"mp4")
            gh.side_effect = [
                "{}",
                json.dumps([[{
                    "id": 17,
                    "body": "<!-- agent-factory:media-staging:fixed -->\n\n"
                    "![swami.png](https://github.com/user-attachments/assets/image)\n\n"
                    "https://github.com/user-attachments/assets/video",
                }]]),
                "{}",
            ]
            urls = _stage_native_attachments(
                "owner/repo", "7", (image, video), "media-token"
            )
        self.assertEqual(urls[str(image)], "https://github.com/user-attachments/assets/image")
        self.assertEqual(urls[str(video)], "https://github.com/user-attachments/assets/video")
        self.assertEqual(
            gh.call_args_list[2].args[0],
            ["api", "repos/owner/repo/issues/comments/17", "-X", "DELETE"],
        )
        self.assertTrue(
            all(call.kwargs.get("token") == "media-token" for call in gh.call_args_list)
        )

    @mock.patch("agent_factory.github_delivery._gh")
    @mock.patch("agent_factory.github_delivery.uuid.uuid4")
    def test_partial_upload_failure_deletes_staging_comment(self, unique_id, gh) -> None:
        unique_id.return_value.hex = "fixed"
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            image.write_bytes(b"png")
            gh.side_effect = [
                RuntimeError("one attachment failed"),
                json.dumps([[{
                    "id": 18,
                    "body": "<!-- agent-factory:media-staging:fixed -->",
                }]]),
                "{}",
            ]
            with self.assertRaisesRegex(RuntimeError, "one attachment failed"):
                _stage_native_attachments(
                    "owner/repo", "7", (image,), "media-token"
                )
        self.assertEqual(
            gh.call_args_list[2].args[0],
            ["api", "repos/owner/repo/issues/comments/18", "-X", "DELETE"],
        )

    def test_native_attachment_preflights_every_file_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "swami.png"
            empty = Path(directory) / "drag.mp4"
            valid.write_bytes(b"png")
            empty.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "empty"):
                _validate_attachments((valid, empty))

    def test_native_attachment_rejects_oversize_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "drag.mp4"
            with video.open("wb") as handle:
                handle.truncate(10 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(ValueError, "10 MB"):
                _validate_attachments((video,))

    def test_native_attachment_caps_count_before_upload(self) -> None:
        attachments = tuple(Path(f"frame-{index}.png") for index in range(51))
        with self.assertRaisesRegex(ValueError, "maximum is 50"):
            _validate_attachments(attachments)

    def test_native_attachment_rejects_duplicate_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            image.write_bytes(b"png")
            with self.assertRaisesRegex(ValueError, "same file twice"):
                _validate_attachments((image, image))

    @mock.patch.dict("os.environ", {}, clear=True)
    @mock.patch("agent_factory.github_delivery._gh")
    def test_native_media_requires_a_separate_user_token(self, gh) -> None:
        head = "a" * 40
        gh.return_value = json.dumps({"headRefOid": head, "body": pending_delivery()})
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            image.write_bytes(b"png")
            with self.assertRaisesRegex(RuntimeError, "MEDIA_UPLOAD_TOKEN is required"):
                publish(
                    "owner/repo", "7", head, "ready", f"![Swami]({image})", (image,)
                )
        self.assertEqual(gh.call_count, 1)

    def test_rewrite_uses_exact_destinations_for_overlapping_names(self) -> None:
        short = "/tmp/shot.png"
        long = "/tmp/before-shot.png"
        rewritten = _rewrite_attachment_references(
            f"![After]({short})\n\n![Before]({long})",
            {
                short: "https://github.com/user-attachments/assets/after",
                long: "https://github.com/user-attachments/assets/before",
            },
        )
        self.assertIn("assets/after)", rewritten)
        self.assertIn("assets/before)", rewritten)
        self.assertNotIn(short, rewritten)
        self.assertNotIn(long, rewritten)

    @mock.patch.dict("os.environ", {"AGENT_FACTORY_MEDIA_UPLOAD_TOKEN": "media-token"})
    @mock.patch("agent_factory.github_delivery._stage_native_attachments")
    @mock.patch("agent_factory.github_delivery._gh")
    def test_preexisting_url_cannot_mask_missing_delivery_media(self, gh, stage) -> None:
        head = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.png"
            second = Path(directory) / "second.png"
            first.write_bytes(b"png")
            second.write_bytes(b"png")
            url_one = "https://github.com/user-attachments/assets/one"
            url_two = "https://github.com/user-attachments/assets/two"
            original = pending_delivery() + f"\n\nOld unrelated media: {url_two}"
            incomplete = format_delivery(
                "ready", f"![First]({url_one})", head=head
            ) + f"\n\nOld unrelated media: {url_two}"
            gh.side_effect = [
                json.dumps({"headRefOid": head, "body": original}),
                json.dumps({"headRefOid": head, "body": original}),
                "{}",
                json.dumps({"headRefOid": head, "body": incomplete}),
            ]
            stage.return_value = {str(first): url_one, str(second): url_two}
            with self.assertRaisesRegex(RuntimeError, "omitted native Builder media"):
                publish(
                    "owner/repo",
                    "7",
                    head,
                    "ready",
                    f"![First]({first})\n\n![Second]({second})",
                    (first, second),
                )

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
