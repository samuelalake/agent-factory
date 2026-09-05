from __future__ import annotations

import unittest

from agent_factory.protocol import decode_data, encode_data, extract_json_reply


class ProtocolTests(unittest.TestCase):
    def test_machine_data_round_trip_is_html_safe(self) -> None:
        value = {"title": "contains -- and unicode café", "findings": []}
        encoded = encode_data(value)
        self.assertNotIn("contains --", encoded)
        self.assertEqual(decode_data(encoded), value)

    def test_invalid_machine_data_is_absent(self) -> None:
        self.assertIsNone(decode_data("<!-- agent-factory:data nope -->"))

    def test_json_reply_accepts_fenced_json(self) -> None:
        self.assertEqual(extract_json_reply("```json\n{\"approve\": true}\n```"), {"approve": True})
