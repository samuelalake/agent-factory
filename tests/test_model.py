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

    def test_gemini_text_and_endpoint(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({
            "candidates": [{"content": {"parts": [{"text": "{\"approve\":true}"}]}}]
        })) as urlopen:
            text = complete("gemini", "gemini-3.5-flash", "system", "user", "key")
        self.assertEqual(text, '{"approve":true}')
        self.assertIn("gemini-3.5-flash:generateContent", urlopen.call_args.args[0].full_url)
        self.assertEqual(urlopen.call_args.args[0].get_header("X-goog-api-key"), "key")

    def test_openrouter_text(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({
            "choices": [{"message": {"content": "{\"approve\":true}"}}]
        })):
            self.assertEqual(complete("openrouter", "free/model", "system", "user", "key"), '{"approve":true}')

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
