from __future__ import annotations

import http.client
import io
import json
import unittest
import urllib.error
from unittest import mock

from agent_factory.model import ModelError, complete


def _response(value: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.read.return_value = json.dumps(value).encode()
    context = mock.MagicMock()
    context.__enter__.return_value = response
    context.__exit__.return_value = False
    return context


class ModelAdapterTests(unittest.TestCase):
    def test_claude_code_uses_subscription_token_without_api_key(self) -> None:
        process = mock.MagicMock(
            returncode=0,
            stdout=json.dumps({"result": '{"approve":true}', "is_error": False}),
            stderr="",
        )
        with (
            mock.patch("agent_factory.model.subprocess.run", return_value=process) as run,
            mock.patch.dict(
                "os.environ",
                {
                    "PATH": "/usr/bin",
                    "ANTHROPIC_API_KEY": "billed-key",
                    "OPENROUTER_API_KEY": "fallback-key",
                    "AGENT_FACTORY_APP_PRIVATE_KEY": "github-key",
                },
                clear=True,
            ),
        ):
            text = complete("claude-code", "opus", "system", "user", "oauth-token")

        self.assertEqual(text, '{"approve":true}')
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["claude", "-p"])
        self.assertIn("--system-prompt", args)
        self.assertIn("--safe-mode", args)
        self.assertIn("--no-session-persistence", args)
        self.assertEqual(run.call_args.kwargs["input"], "user")
        self.assertEqual(
            run.call_args.kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"],
            "oauth-token",
        )
        self.assertNotIn("ANTHROPIC_API_KEY", run.call_args.kwargs["env"])
        self.assertNotIn("OPENROUTER_API_KEY", run.call_args.kwargs["env"])
        self.assertNotIn("AGENT_FACTORY_APP_PRIVATE_KEY", run.call_args.kwargs["env"])
        self.assertEqual(run.call_args.kwargs["env"]["PATH"], "/usr/bin")

    def test_claude_code_rejects_inline_images(self) -> None:
        with self.assertRaisesRegex(ModelError, "does not support inline image"):
            complete(
                "claude-code",
                "opus",
                "system",
                "user",
                "oauth-token",
                image_urls=("data:image/png;base64,aGVsbG8=",),
            )

    def test_claude_code_does_not_reflect_stderr(self) -> None:
        process = mock.MagicMock(returncode=1, stdout="", stderr="secret prompt material")
        with mock.patch("agent_factory.model.subprocess.run", return_value=process):
            with self.assertRaisesRegex(ModelError, "exited with status 1") as raised:
                complete("claude-code", "opus", "system", "user", "oauth-token")
        self.assertNotIn("secret", str(raised.exception))

    def test_transient_model_capacity_retries_before_provider_fallback(self) -> None:
        def unavailable() -> urllib.error.HTTPError:
            return urllib.error.HTTPError(
                "https://example.test",
                503,
                "Unavailable",
                {"Retry-After": "1"},
                io.BytesIO(b'{"error":"high demand"}'),
            )

        with (
            mock.patch(
                "urllib.request.urlopen",
                side_effect=[unavailable(), unavailable(), _response({
                    "candidates": [{"content": {"parts": [{"text": "{\"approve\":true}"}]}}]
                })],
            ) as urlopen,
            mock.patch("agent_factory.model.time.sleep") as sleep,
        ):
            text = complete("gemini", "gemini-3.6-flash", "system", "user", "key")
        self.assertEqual(text, '{"approve":true}')
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 1])

    def test_anthropic_text(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({
            "content": [{"type": "text", "text": "{\"approve\":true}"}]
        })):
            self.assertEqual(complete("anthropic", "model", "system", "user", "key"), '{"approve":true}')

    def test_anthropic_visual_content_uses_native_image_blocks(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({
            "content": [{"type": "text", "text": "{}"}]
        })) as urlopen:
            complete(
                "anthropic", "model", "system", "user", "key",
                image_urls=("data:image/jpeg;base64,aGVsbG8=",),
            )
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload["messages"][0]["content"][1], {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "aGVsbG8=",
            },
        })

    def test_gemini_text_and_endpoint(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({
            "candidates": [{"content": {"parts": [{"text": "{\"approve\":true}"}]}}]
        })) as urlopen:
            text = complete("gemini", "gemini-3.5-flash", "system", "user", "key")
        self.assertEqual(text, '{"approve":true}')
        self.assertIn("gemini-3.5-flash:generateContent", urlopen.call_args.args[0].full_url)
        self.assertEqual(urlopen.call_args.args[0].get_header("X-goog-api-key"), "key")

    def test_gemini_visual_content_uses_inline_data(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}]
        })) as urlopen:
            complete(
                "gemini", "model", "system", "user", "key",
                image_urls=("data:image/webp;base64,aGVsbG8=",),
            )
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload["contents"][0]["parts"][1], {
            "inline_data": {"mime_type": "image/webp", "data": "aGVsbG8="}
        })

    def test_openrouter_text(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({
            "choices": [{"message": {"content": "{\"approve\":true}"}}]
        })):
            self.assertEqual(complete("openrouter", "free/model", "system", "user", "key"), '{"approve":true}')

    def test_openrouter_visual_content_uses_validated_data_urls(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({
            "choices": [{"message": {"content": "{\"approve\":false}"}}]
        })) as urlopen:
            complete(
                "openrouter",
                "visual/model",
                "system",
                "compare these",
                "key",
                image_urls=("data:image/png;base64,aGVsbG8=",),
            )
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload["messages"][1]["content"], [
            {"type": "text", "text": "compare these"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,aGVsbG8="},
            },
        ])

    def test_visual_content_rejects_non_image_data(self) -> None:
        with self.assertRaisesRegex(ModelError, "base64 image data URL"):
            complete(
                "openrouter",
                "visual/model",
                "system",
                "user",
                "key",
                image_urls=("https://example.test/image.png",),
            )

    def test_minimax_text_and_endpoint(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({
            "choices": [{"message": {
                "reasoning_details": [{"type": "reasoning.text", "text": "private reasoning"}],
                "content": "{\"approve\":true}",
            }}]
        })) as urlopen:
            text = complete("minimax", "MiniMax-M2.7", "system", "user", "key")
        self.assertEqual(text, '{"approve":true}')
        self.assertEqual(
            urlopen.call_args.args[0].full_url,
            "https://api.minimax.io/v1/chat/completions",
        )
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertNotIn("response_format", payload)
        self.assertIs(payload["reasoning_split"], True)

    def test_http_error_does_not_reflect_provider_body(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.test",
            429,
            "limited",
            {"Retry-After": "10", "x-ratelimit-remaining": "0"},
            io.BytesIO(b'{"error":"secret reflected prompt"}'),
        )
        with (
            mock.patch("urllib.request.urlopen", side_effect=[error, error, error]),
            mock.patch("agent_factory.model.time.sleep"),
        ):
            with self.assertRaises(ModelError) as raised:
                complete("minimax", "MiniMax-M2.7", "system", "user", "secret")
        self.assertRegex(
            str(raised.exception), "model HTTP 429.*retry_after=10.*remaining=0"
        )
        self.assertNotIn("secret", str(raised.exception))

    def test_nvidia_text_and_endpoint(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({
            "choices": [{"message": {"content": "{\"approve\":true}"}}]
        })) as urlopen:
            text = complete("nvidia", "moonshotai/kimi-k3", "system", "user", "key")
        self.assertEqual(text, '{"approve":true}')
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://integrate.api.nvidia.com/v1/chat/completions")
        self.assertEqual(urlopen.call_args.args[0].get_header("Authorization"), "Bearer key")

    def test_nvidia_visual_content_uses_openai_shape(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({
            "choices": [{"message": {"content": "{}"}}]
        })) as urlopen:
            complete(
                "nvidia", "visual/model", "system", "user", "key",
                image_urls=("data:image/png;base64,aGVsbG8=",),
            )
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(
            payload["messages"][1]["content"][1]["image_url"]["url"],
            "data:image/png;base64,aGVsbG8=",
        )

    def test_unknown_provider_fails(self) -> None:
        with self.assertRaisesRegex(ModelError, "unsupported"):
            complete("mystery", "model", "system", "user", "key")

    def test_dropped_connection_becomes_provider_failure(self) -> None:
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("closed without response"),
        ):
            with self.assertRaisesRegex(ModelError, "model transport failed"):
                complete("gemini", "gemini-3.5-flash", "system", "user", "key")

    def test_invalid_provider_json_becomes_provider_failure(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b"not-json"
        context = mock.MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with mock.patch("urllib.request.urlopen", return_value=context):
            with self.assertRaisesRegex(ModelError, "model returned invalid JSON"):
                complete("gemini", "gemini-3.5-flash", "system", "user", "key")
