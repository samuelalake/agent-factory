from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_factory.github_delivery import (
    DELIVERY_END,
    DELIVERY_PROVENANCE,
    DELIVERY_START,
    authenticated_delivery_evidence,
    delivery_head,
    delivery_evidence_manifest,
    delivery_status,
    format_delivery,
    pending_delivery,
    publish,
    _evidence_manifest,
    _publish_evidence_provenance,
    _rewrite_attachment_references,
    _stage_native_attachments,
    _validate_attachments,
    replace_delivery,
    wait_for_delivery,
)


def _provenance_response(head: str, manifest_marker: str) -> str:
    body = (
        "<!-- agent-factory:builder-evidence-provenance -->\n"
        "<details><summary>Builder evidence provenance</summary>\n\n"
        f"Authenticated media manifest for `{head}`.\n\n{manifest_marker}\n\n"
        "</details>"
    )
    return json.dumps({
        "id": 91,
        "body": body,
        "user": {"type": "Bot", "login": "agent-factory-builder[bot]"},
    })


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
            urls = {
                str(image): (
                    "https://github.com/user-attachments/assets/"
                    "63712384-e836-41c5-aaf8-5c7149499b3f"
                ),
                str(video): (
                    "https://github.com/user-attachments/assets/"
                    "e75bec00-fa65-4fb6-9b41-9cf55f4eda5e"
                ),
            }
            native_body = format_delivery(
                "ready",
                f"![Swami]({urls[str(image)]})\n\n"
                f"{urls[str(video)]}\n\n"
                f"{_evidence_manifest('owner/repo', '7', head, (image, video), urls)}",
                head=head,
            )
            manifest_marker = _evidence_manifest(
                "owner/repo", "7", head, (image, video), urls
            )
            gh.side_effect = [
                json.dumps({"headRefOid": head, "body": original}),
                json.dumps([[]]),
                _provenance_response(head, manifest_marker),
                json.dumps({"headRefOid": head, "body": original}),
                "{}",
                json.dumps({"headRefOid": head, "body": native_body}),
            ]
            stage.return_value = urls
            publish(
                "owner/repo",
                "7",
                head,
                "ready",
                f"![Swami]({image})\n\n![]({video})",
                (image, video),
            )
        stage.assert_called_once_with("owner/repo", "7", (image, video), "media-token")
        patch_call = gh.call_args_list[4]
        self.assertEqual(
            patch_call.args[0][:3], ["api", "repos/owner/repo/pulls/7", "-X"]
        )
        payload = json.loads(patch_call.kwargs["stdin"])
        self.assertIn(f"![Swami]({urls[str(image)]})", payload["body"])
        self.assertIn(f"\n{urls[str(video)]}\n", payload["body"])
        manifest = delivery_evidence_manifest(
            payload["body"], expected_repo="owner/repo", expected_pr=7,
            expected_head=head,
        )
        self.assertEqual(set(manifest or {}), set(urls.values()))
        self.assertEqual(manifest[urls[str(image)]]["content_type"], "image/png")
        self.assertEqual(manifest[urls[str(video)]]["content_type"], "video/mp4")

    def test_delivery_evidence_manifest_is_bound_to_head_and_bytes(self) -> None:
        head = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            image.write_bytes(b"png")
            url = (
                "https://github.com/user-attachments/assets/"
                "63712384-e836-41c5-aaf8-5c7149499b3f"
            )
            marker = _evidence_manifest(
                "owner/repo", "7", head, (image,), {str(image): url}
            )
        manifest = delivery_evidence_manifest(
            marker, expected_repo="owner/repo", expected_pr=7, expected_head=head
        )
        self.assertEqual(
            manifest[url]["sha256"],
            "8f8cbb7dcf46e0bc7d53265749a6c17d116093a6ba95e442764060c76fd4a86c",
        )
        self.assertIsNone(delivery_evidence_manifest(
            marker, expected_repo="owner/repo", expected_pr=7,
            expected_head="b" * 40,
        ))

    def test_authenticated_provenance_requires_bot_and_matches_canonical_body(self) -> None:
        head = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            image.write_bytes(b"png")
            url = "https://github.com/user-attachments/assets/asset"
            marker = _evidence_manifest(
                "owner/repo", "7", head, (image,), {str(image): url}
            )
            other_url = "https://github.com/user-attachments/assets/other"
            other_marker = _evidence_manifest(
                "owner/repo", "7", head, (image,), {str(image): other_url}
            )
        trusted = {
            "body": DELIVERY_PROVENANCE + "\n" + marker,
            "user": {"type": "Bot", "login": "agent-factory-builder[bot]"},
        }
        manifest = authenticated_delivery_evidence(
            [[trusted]], expected_repo="owner/repo", expected_pr=7,
            expected_head=head, builder_app_login="agent-factory-builder[bot]",
            expected_manifest=delivery_evidence_manifest(
                marker, expected_repo="owner/repo", expected_pr=7, expected_head=head
            ),
        )
        self.assertEqual(tuple(manifest or {}), (url,))
        copied = {**trusted, "user": {"type": "User", "login": "attacker"}}
        self.assertIsNone(authenticated_delivery_evidence(
            [[copied]], expected_repo="owner/repo", expected_pr=7,
            expected_head=head, builder_app_login="agent-factory-builder[bot]",
            expected_manifest=manifest,
        ))
        self.assertEqual(manifest, authenticated_delivery_evidence(
            [[trusted, trusted]], expected_repo="owner/repo", expected_pr=7,
            expected_head=head, builder_app_login="agent-factory-builder[bot]",
            expected_manifest=manifest,
        ))
        conflicting = {**trusted, "body": DELIVERY_PROVENANCE + "\n" + other_marker}
        self.assertEqual(manifest, authenticated_delivery_evidence(
            [[trusted, conflicting]], expected_repo="owner/repo", expected_pr=7,
            expected_head=head, builder_app_login="agent-factory-builder[bot]",
            expected_manifest=manifest,
        ))

    @mock.patch("agent_factory.github_delivery._gh")
    def test_stale_provenance_is_not_overwritten_by_another_head(self, gh) -> None:
        head = "a" * 40
        old_head = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            image.write_bytes(b"png")
            url = "https://github.com/user-attachments/assets/asset"
            urls = {str(image): url}
            marker = _evidence_manifest("owner/repo", "7", head, (image,), urls)
            old_marker = _evidence_manifest(
                "owner/repo", "7", old_head, (image,), urls
            )
        existing = json.loads(_provenance_response(old_head, old_marker))
        existing["id"] = 81
        gh.side_effect = [json.dumps([[existing]]), _provenance_response(head, marker)]
        _publish_evidence_provenance(
            "owner/repo", "7", head, marker, "agent-factory-builder[bot]"
        )
        self.assertEqual(
            gh.call_args_list[1].args[0][1], "repos/owner/repo/issues/7/comments"
        )
        self.assertIn("POST", gh.call_args_list[1].args[0])

    @mock.patch("agent_factory.github_delivery._gh")
    def test_current_head_provenance_is_upserted(self, gh) -> None:
        head = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "swami.png"
            image.write_bytes(b"png")
            url = "https://github.com/user-attachments/assets/asset"
            marker = _evidence_manifest(
                "owner/repo", "7", head, (image,), {str(image): url}
            )
        existing = json.loads(_provenance_response(head, marker))
        gh.side_effect = [json.dumps([[existing]]), _provenance_response(head, marker)]
        _publish_evidence_provenance(
            "owner/repo", "7", head, marker, "agent-factory-builder[bot]"
        )
        self.assertEqual(
            gh.call_args_list[1].args[0][1], "repos/owner/repo/issues/comments/91"
        )
        self.assertIn("PATCH", gh.call_args_list[1].args[0])

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
            urls = {str(image): "https://github.com/user-attachments/assets/image"}
            marker = _evidence_manifest("owner/repo", "7", old_head, (image,), urls)
            gh.side_effect = [
                json.dumps({"headRefOid": old_head, "body": original}),
                json.dumps([[]]),
                _provenance_response(old_head, marker),
                json.dumps({"headRefOid": new_head, "body": newer}),
            ]
            stage.return_value = urls
            with self.assertRaisesRegex(RuntimeError, "refusing stale"):
                publish(
                    "owner/repo", "7", old_head, "ready", f"![Swami]({image})", (image,)
                )
        self.assertEqual(gh.call_count, 4)
        self.assertFalse(any(
            call.args[0][:2] == ["api", "repos/owner/repo/pulls/7"]
            for call in gh.call_args_list
        ))

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
            urls = {str(first): url_one, str(second): url_two}
            marker = _evidence_manifest("owner/repo", "7", head, (first, second), urls)
            gh.side_effect = [
                json.dumps({"headRefOid": head, "body": original}),
                json.dumps([[]]),
                _provenance_response(head, marker),
                json.dumps({"headRefOid": head, "body": original}),
                "{}",
                json.dumps({"headRefOid": head, "body": incomplete}),
            ]
            stage.return_value = urls
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
