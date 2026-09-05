from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from agent_factory import app_auth


def _response(value: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.read.return_value = json.dumps(value).encode()
    context = mock.MagicMock()
    context.__enter__.return_value = response
    context.__exit__.return_value = False
    return context


class AppAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        app_auth._CACHE.clear()

    def test_requires_generic_credentials(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(app_auth.AppAuthError, "AGENT_FACTORY_APP_ID"):
                app_auth.get_installation_token("owner/repo")

    def test_rejects_bad_repository_slug_before_network(self) -> None:
        with self.assertRaisesRegex(app_auth.AppAuthError, "owner/name"):
            app_auth.get_installation_token("repo")

    def test_mints_and_caches_installation_token(self) -> None:
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with mock.patch.object(app_auth.jwt, "encode", return_value="app.jwt") as encode, mock.patch(
            "urllib.request.urlopen"
        ) as urlopen:
            urlopen.side_effect = [
                _response({"id": 42}),
                _response({"token": "ghs_token", "expires_at": expiry}),
            ]
            first = app_auth.get_installation_token(
                "owner/repo", app_id="7", private_key="private"
            )
            second = app_auth.get_installation_token("owner/repo")

        self.assertEqual(first, "ghs_token")
        self.assertEqual(second, "ghs_token")
        self.assertEqual(urlopen.call_count, 2)
        claims = encode.call_args.args[0]
        self.assertEqual(claims["iss"], "7")
        self.assertEqual(encode.call_args.kwargs["algorithm"], "RS256")

    def test_http_failure_has_bounded_diagnostic(self) -> None:
        error = __import__("urllib.error").error.HTTPError(
            "https://api.github.com/x", 403, "Forbidden", {}, io.BytesIO(b"denied")
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(app_auth.AppAuthError, "403 denied"):
                app_auth._request("https://api.github.com/x", "GET", "jwt")
