from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from agent_factory.provider_smoke import (
    ProviderSmokeError,
    _request,
    discover_models,
    probe_model,
    run,
    select_candidates,
)


class _Response:
    def __init__(self, value: dict):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ProviderSmokeTests(unittest.TestCase):
    def test_nvidia_catalog_uses_v1_models_endpoint(self) -> None:
        with mock.patch(
            "agent_factory.provider_smoke._request",
            return_value={"data": [{"id": "vendor/model"}]},
        ) as request:
            self.assertEqual(discover_models("nvidia", "secret"), ["vendor/model"])
        self.assertEqual(request.call_args.args[0], "https://integrate.api.nvidia.com/v1/models")

    def test_requested_model_precedes_discovered_coding_models(self) -> None:
        result = select_candidates(
            ["poolside/laguna-xs-2.1", "z-ai/glm-5.2"],
            ["moonshotai/kimi-k3"],
            2,
        )
        self.assertEqual(result, ["moonshotai/kimi-k3", "poolside/laguna-xs-2.1"])

    def test_probe_requires_and_consumes_tool_call(self) -> None:
        first = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "write_probe",
                                    "arguments": '{"value":"ready"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
        second = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "PROBE_COMPLETE agent-factory-smoke-v1",
                    }
                }
            ]
        }
        with mock.patch(
            "agent_factory.provider_smoke._request", side_effect=[first, second]
        ) as request:
            probe_model("nvidia", "model", "secret")
        self.assertEqual(request.call_count, 2)
        sent_messages = request.call_args.kwargs["payload"]["messages"]
        self.assertEqual(sent_messages[-1]["role"], "tool")

    def test_builder_shape_uses_full_tool_schema_and_configured_limits(self) -> None:
        first = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": "private reasoning"}
                        ],
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "list_files",
                                    "arguments": '{"pattern":"*"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
        second = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "PROBE_COMPLETE agent-factory-smoke-v1",
                    }
                }
            ]
        }
        with mock.patch(
            "agent_factory.provider_smoke._request", side_effect=[first, second]
        ) as request:
            probe_model(
                "minimax", "model", "secret", builder_shape=True,
                max_output_tokens=1024, prompt_bytes=4096,
            )
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertIs(payload["reasoning_split"], True)
        self.assertEqual(len(payload["tools"]), 6)
        self.assertGreaterEqual(len(payload["messages"][0]["content"].encode()), 4096)
        self.assertEqual(
            payload["messages"][1]["reasoning_details"][0]["text"],
            "private reasoning",
        )

    def test_visual_smoke_uses_the_production_multimodal_shape(self) -> None:
        first = {
            "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call-1", "function": {"name": "write_probe", "arguments": '{"value":"red"}'}}
            ]}}]
        }
        second = {
            "choices": [{"message": {"role": "assistant", "content": "PROBE_COMPLETE agent-factory-smoke-v1"}}]
        }
        with mock.patch(
            "agent_factory.provider_smoke._request", side_effect=[first, second]
        ) as request:
            probe_model("openrouter", "vision-model", "secret", visual_input=True)
        first_payload = request.call_args_list[0].kwargs["payload"]
        self.assertEqual(first_payload["tool_choice"], "required")
        self.assertEqual(request.call_args_list[1].kwargs["payload"]["tool_choice"], "auto")
        content = first_payload["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_http_error_reports_only_safe_rate_metadata(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.test",
            429,
            "limited",
            {"Retry-After": "10", "x-ratelimit-remaining": "0"},
            io.BytesIO(json.dumps({"error": {"message": "secret reflected"}}).encode()),
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ProviderSmokeError) as raised:
                _request("https://example.test", "secret", payload={})
        self.assertRegex(str(raised.exception), "HTTP 429.*retry_after=10.*remaining=0")
        self.assertNotIn("secret", str(raised.exception))

    def test_invalid_json_is_reported_safely(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"not-json")):
            with self.assertRaisesRegex(ProviderSmokeError, "invalid JSON"):
                _request("https://example.test", "secret")

    def test_catalog_shape_is_validated(self) -> None:
        with mock.patch(
            "agent_factory.provider_smoke._request", return_value={"data": ["bad"]}
        ):
            with self.assertRaisesRegex(ProviderSmokeError, "invalid model catalog"):
                discover_models("nvidia", "secret")

    def test_discovery_failure_does_not_block_requested_model(self) -> None:
        with (
            mock.patch.dict("os.environ", {"PROVIDER_API_KEY": "secret"}, clear=True),
            mock.patch(
                "agent_factory.provider_smoke.discover_models",
                side_effect=ProviderSmokeError("catalog unavailable"),
            ),
            mock.patch("agent_factory.provider_smoke.probe_model") as probe,
            mock.patch("builtins.print"),
        ):
            selected = run("nvidia", ["vendor/model"], discover=True, limit=1)
        self.assertEqual(selected, "vendor/model")
        probe.assert_called_once()

    def test_probe_rejects_multiple_initial_tool_calls(self) -> None:
        call = {
            "id": "call-1",
            "function": {"name": "write_probe", "arguments": '{"value":"ready"}'},
        }
        first = {"choices": [{"message": {"tool_calls": [call, call]}}]}
        with mock.patch("agent_factory.provider_smoke._request", return_value=first):
            with self.assertRaisesRegex(ProviderSmokeError, "required tool call"):
                probe_model("nvidia", "model", "secret")

    def test_probe_rejects_second_tool_call_instead_of_acknowledgement(self) -> None:
        first = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "write_probe",
                                    "arguments": '{"value":"ready"}',
                                },
                            }
                        ]
                    }
                }
            ]
        }
        second = {
            "choices": [
                {"message": {"tool_calls": [{"id": "call-2", "function": {}}]}}
            ]
        }
        with mock.patch(
            "agent_factory.provider_smoke._request", side_effect=[first, second]
        ):
            with self.assertRaisesRegex(ProviderSmokeError, "acknowledge"):
                probe_model("nvidia", "model", "secret")

    def test_probe_rejects_wrong_acknowledgement(self) -> None:
        first = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "write_probe",
                                    "arguments": '{"value":"ready"}',
                                },
                            }
                        ]
                    }
                }
            ]
        }
        second = {"choices": [{"message": {"content": "done"}}]}
        with mock.patch(
            "agent_factory.provider_smoke._request", side_effect=[first, second]
        ):
            with self.assertRaisesRegex(ProviderSmokeError, "acknowledge"):
                probe_model("nvidia", "model", "secret")

    def test_probe_rejects_malformed_chat_response_shapes(self) -> None:
        with mock.patch(
            "agent_factory.provider_smoke._request",
            return_value={"choices": [None]},
        ):
            with self.assertRaisesRegex(ProviderSmokeError, "invalid chat response"):
                probe_model("nvidia", "model", "secret")

        for malformed_call in (None, {"function": None}, {"function": {}}):
            with self.subTest(malformed_call=malformed_call), mock.patch(
                "agent_factory.provider_smoke._request",
                return_value={
                    "choices": [{"message": {"tool_calls": [malformed_call]}}]
                },
            ):
                with self.assertRaisesRegex(ProviderSmokeError, "required tool call"):
                    probe_model("nvidia", "model", "secret")

        first = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "write_probe",
                                    "arguments": '{"value":"ready"}',
                                },
                            }
                        ]
                    }
                }
            ]
        }
        with mock.patch(
            "agent_factory.provider_smoke._request",
            side_effect=[first, {"choices": [None]}],
        ):
            with self.assertRaisesRegex(ProviderSmokeError, "acknowledge"):
                probe_model("nvidia", "model", "secret")

    def test_direct_api_rejects_unbounded_requests(self) -> None:
        with self.assertRaisesRegex(ProviderSmokeError, "max_output_tokens"):
            probe_model("nvidia", "model", "secret", max_output_tokens=8193)
        with self.assertRaisesRegex(ProviderSmokeError, "prompt_bytes"):
            probe_model("nvidia", "model", "secret", prompt_bytes=200001)
        with self.assertRaisesRegex(ProviderSmokeError, "limit"):
            run("nvidia", ["model"], discover=False, limit=9)


if __name__ == "__main__":
    unittest.main()
