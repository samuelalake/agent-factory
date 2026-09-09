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
                json.dumps({"headRefOid": head, "body": original}),
                "{}",
                json.dumps({"headRefOid": head, "body": native_body}),
            ]
            upload.return_value = {
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
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args, ("owner/repo", (image, video), "user-token"))
        payload = json.loads(gh.call_args_list[2].kwargs["stdin"])
        self.assertIn("https://github.com/user-attachments/assets/image", payload["body"])
        self.assertIn("\nhttps://github.com/user-attachments/assets/video\n", payload["body"])

    @mock.patch.dict("os.environ", {"AGENT_FACTORY_MEDIA_UPLOAD_TOKEN": "user-token"})
    @mock.patch("agent_factory.github_delivery._publish_native_attachments")
    @mock.patch("agent_factory.github_delivery._gh")
    def test_native_media_never_overwrites_a_new_builder_revision(self, gh, upload) -> None:
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
            upload.return_value = {
                str(image): "https://github.com/user-attachments/assets/image"
            }
            with self.assertRaisesRegex(RuntimeError, "refusing stale Builder evidence"):
                publish(
                    "owner/repo", "7", old_head, "ready", f"![Swami]({image})", (image,)
                )
        self.assertEqual(gh.call_count, 2)
        self.assertFalse(any("PATCH" in call.args[0] for call in gh.call_args_list))

    @mock.patch.dict("os.environ", {"AGENT_FACTORY_MEDIA_UPLOAD_TOKEN": "user-token"})
    @mock.patch("agent_factory.github_delivery._publish_native_attachments")
    @mock.patch("agent_factory.github_delivery._gh")
    def test_native_publication_fails_closed_on_non_native_urls(self, gh, upload) -> None:
        head = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "drag.mp4"
            video.write_bytes(b"mp4")
            original = pending_delivery()
            gh.side_effect = [
                json.dumps({"headRefOid": head, "body": original}),
            ]
            upload.return_value = {str(video): "https://example.test/drag.mp4"}
            with self.assertRaisesRegex(RuntimeError, "native Builder media"):
                publish(
                    "owner/repo", "7", head, "ready", f"![]({video})", (video,)
                )

    @mock.patch("agent_factory.github_delivery.urlopen")
    @mock.patch("agent_factory.github_delivery._api")
    def test_native_attachment_upload_uses_separate_token(self, api, open_url) -> None:
        api.return_value = {"id": 1234}
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "url": "https://github.com/user-attachments/assets/image"
        }).encode("utf-8")
        open_url.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            image.write_bytes(b"png")
            urls = _publish_native_attachments("owner/repo", (image,), "user-token")
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url,
            "https://uploads.github.com/user-attachments/assets?"
            "name=swami.png&content_type=image%2Fpng&repository_id=1234",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer user-token")
        self.assertEqual(request.data, b"png")
        self.assertEqual(urls[str(image)], "https://github.com/user-attachments/assets/image")

    @mock.patch("agent_factory.github_delivery.urlopen")
    @mock.patch("agent_factory.github_delivery._api")
    def test_native_attachment_preflights_every_file_before_upload(self, api, open_url) -> None:
        api.return_value = {"id": 1234}
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "swami.png"
            empty = Path(directory) / "drag.mp4"
            valid.write_bytes(b"png")
            empty.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "empty"):
                _publish_native_attachments(
                    "owner/repo", (valid, empty), "user-token"
                )
        api.assert_not_called()
        open_url.assert_not_called()

    @mock.patch("agent_factory.github_delivery.urlopen")
    @mock.patch("agent_factory.github_delivery._api")
    def test_native_attachment_rejects_oversize_before_read_or_upload(self, api, open_url) -> None:
        api.return_value = {"id": 1234}
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "drag.mp4"
            with video.open("wb") as handle:
                handle.truncate(10 * 1024 * 1024 + 1)
            with mock.patch.object(Path, "read_bytes") as read_bytes:
                with self.assertRaisesRegex(ValueError, "10 MB"):
                    _publish_native_attachments("owner/repo", (video,), "user-token")
            read_bytes.assert_not_called()
        api.assert_not_called()
        open_url.assert_not_called()

    @mock.patch("agent_factory.github_delivery.urlopen")
    @mock.patch("agent_factory.github_delivery._api")
    def test_native_attachment_caps_count_before_upload(self, api, open_url) -> None:
        attachments = tuple(Path(f"frame-{index}.png") for index in range(51))
        with self.assertRaisesRegex(ValueError, "maximum is 50"):
            _publish_native_attachments("owner/repo", attachments, "user-token")
        api.assert_not_called()
        open_url.assert_not_called()

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
