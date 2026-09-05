from __future__ import annotations

import json
import unittest
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

    def test_unknown_provider_fails(self) -> None:
        with self.assertRaisesRegex(ModelError, "unsupported"):
            complete("mystery", "model", "system", "user", "key")
