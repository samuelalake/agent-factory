from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import io
import json
import subprocess
import urllib.error
from unittest import mock

from agent_factory.nvidia_builder import (
    ModelCostBudget,
    NvidiaBuilderError,
    _execute_tool,
    _inside,
    _post,
    run_openai_builder,
)


class NvidiaBuilderTests(unittest.TestCase):
    def test_paths_cannot_escape_repository_or_enter_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(NvidiaBuilderError, "escaped"):
                _inside(root, "../secret")
            with self.assertRaisesRegex(NvidiaBuilderError, "git"):
                _inside(root, ".git/config")

    def test_write_and_exact_replace_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _execute_tool(root, "write_file", {"path": "src/demo.txt", "content": "before"})
            result = _execute_tool(
                root,
                "replace_text",
                {"path": "src/demo.txt", "old": "before", "new": "after"},
            )
            self.assertEqual(result["replacements"], 1)
            self.assertEqual((root / "src/demo.txt").read_text(), "after")

    def test_command_policy_denies_publication_and_secret_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for command in ("git push origin main", "gh pr create", "printenv", "sudo true", "rm -rf out"):
                with self.subTest(command=command), self.assertRaisesRegex(NvidiaBuilderError, "rejected"):
                    _execute_tool(root, "run_command", {"command": command})

    def test_post_retries_rate_limit_with_server_delay(self) -> None:
        limited = __import__("urllib.error").error.HTTPError(
            "https://example.test", 429, "limited", {"Retry-After": "2"}, None
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with (
            mock.patch("agent_factory.nvidia_builder.urllib.request.urlopen", side_effect=[limited, response]),
            mock.patch("agent_factory.nvidia_builder.json.load", return_value={"choices": []}),
            mock.patch("agent_factory.nvidia_builder.time.sleep") as sleep,
        ):
            self.assertEqual(_post("model", [], "key", 30), {"choices": []})
        sleep.assert_called_once_with(2)

    def test_post_retries_transient_service_capacity(self) -> None:
        unavailable = __import__("urllib.error").error.HTTPError(
            "https://example.test", 503, "unavailable", {}, None
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with (
            mock.patch(
                "agent_factory.nvidia_builder.urllib.request.urlopen",
                side_effect=[unavailable, response],
            ),
            mock.patch("agent_factory.nvidia_builder.json.load", return_value={"choices": []}),
            mock.patch("agent_factory.nvidia_builder.time.sleep") as sleep,
        ):
            self.assertEqual(_post("model", [], "key", 30), {"choices": []})
        sleep.assert_called_once_with(30)

    def test_post_retries_minimax_overload(self) -> None:
        overloaded = __import__("urllib.error").error.HTTPError(
            "https://example.test", 529, "overloaded", {}, io.BytesIO(b'{}')
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with (
            mock.patch(
                "agent_factory.nvidia_builder.urllib.request.urlopen",
                side_effect=[overloaded, response],
            ),
            mock.patch("agent_factory.nvidia_builder.json.load", return_value={"choices": []}),
            mock.patch("agent_factory.nvidia_builder.time.sleep") as sleep,
        ):
            self.assertEqual(
                _post("MiniMax-M2.7", [], "key", 30, provider="minimax"),
                {"choices": []},
            )
        sleep.assert_called_once_with(30)

    def test_openrouter_in_flight_budget_honors_nested_retry_after(self) -> None:
        detail = {
            "error": {
                "metadata": {
                    "reason": "in_flight_budget_exhausted",
                    "headers": {"Retry-After": "120"},
                }
            }
        }
        limited = __import__("urllib.error").error.HTTPError(
            "https://example.test", 402, "limited", {}, io.BytesIO(json.dumps(detail).encode())
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with (
            mock.patch(
                "agent_factory.nvidia_builder.urllib.request.urlopen",
                side_effect=[limited, response],
            ),
            mock.patch("agent_factory.nvidia_builder.json.load", return_value={"choices": []}),
            mock.patch("agent_factory.nvidia_builder.time.sleep") as sleep,
        ):
            self.assertEqual(
                _post(
                    "qwen/qwen3-coder-next",
                    [],
                    "key",
                    30,
                    provider="openrouter",
                    input_cost_per_million=1,
                    output_cost_per_million=2,
                ),
                {"choices": []},
            )
        sleep.assert_called_once_with(120)

    def test_minimax_uses_its_openai_compatible_endpoint(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with (
            mock.patch("agent_factory.nvidia_builder.urllib.request.urlopen", return_value=response) as open_url,
            mock.patch("agent_factory.nvidia_builder.json.load", return_value={"choices": []}),
        ):
            _post("MiniMax-M2.7", [], "key", 30, provider="minimax")
        self.assertEqual(
            open_url.call_args.args[0].full_url,
            "https://api.minimax.io/v1/chat/completions",
        )
        payload = json.loads(open_url.call_args.args[0].data)
        self.assertIs(payload["reasoning_split"], True)

    def test_other_providers_do_not_request_split_reasoning(self) -> None:
        for provider in ("nvidia",):
            with self.subTest(provider=provider):
                response = mock.MagicMock()
                response.__enter__.return_value = response
                with (
                    mock.patch(
                        "agent_factory.nvidia_builder.urllib.request.urlopen",
                        return_value=response,
                    ) as open_url,
                    mock.patch(
                        "agent_factory.nvidia_builder.json.load",
                        return_value={"choices": []},
                    ),
                ):
                    _post("model", [], "key", 30, provider=provider)
                payload = json.loads(open_url.call_args.args[0].data)
                self.assertNotIn("reasoning_split", payload)

    def test_openrouter_builder_rejects_unpriced_requests(self) -> None:
        with self.assertRaisesRegex(NvidiaBuilderError, "positive input and output prices"):
            run_openai_builder(
                "task",
                Path("."),
                provider="openrouter",
                model="model",
                api_key="key",
                max_requests=1,
                timeout_seconds=60,
                max_cost_usd=1,
                input_cost_per_million=0,
                output_cost_per_million=0,
            )

    def test_openrouter_rejects_a_partial_price_ceiling(self) -> None:
        with self.assertRaisesRegex(NvidiaBuilderError, "positive input and output prices"):
            _post(
                "model",
                [],
                "key",
                30,
                provider="openrouter",
                input_cost_per_million=4,
            )

    def test_openrouter_caps_endpoint_prices_to_factory_estimate(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with (
            mock.patch(
                "agent_factory.nvidia_builder.urllib.request.urlopen",
                return_value=response,
            ) as open_url,
            mock.patch(
                "agent_factory.nvidia_builder.json.load",
                return_value={"choices": []},
            ),
        ):
            _post(
                "model",
                [],
                "key",
                30,
                provider="openrouter",
                input_cost_per_million=4,
                output_cost_per_million=15,
            )
        payload = json.loads(open_url.call_args.args[0].data)
        self.assertEqual(
            payload["provider"],
            {"max_price": {"prompt": 4, "completion": 15}},
        )

    def test_http_error_body_cannot_reach_builder_issue_detail(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.test",
            400,
            "bad request",
            {},
            io.BytesIO(b'{"error":"secret reflected repository prompt"}'),
        )
        with mock.patch(
            "agent_factory.nvidia_builder.urllib.request.urlopen", side_effect=error
        ):
            with self.assertRaises(NvidiaBuilderError) as raised:
                _post("MiniMax-M2.7", [], "key", 30, provider="minimax")
        from agent_factory.github_builder import _blocked_detail

        issue_detail = _blocked_detail(str(raised.exception), "gemini", "minimax")
        self.assertEqual(issue_detail, "minimax HTTP 400")
        self.assertNotIn("secret", issue_detail)
        self.assertNotIn("repository prompt", issue_detail)

    def test_post_uses_configured_output_reservation(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with (
            mock.patch("agent_factory.nvidia_builder.urllib.request.urlopen", return_value=response) as open_url,
            mock.patch("agent_factory.nvidia_builder.json.load", return_value={"choices": []}),
        ):
            _post("model", [], "key", 30, max_output_tokens=2048)
        payload = json.loads(open_url.call_args.args[0].data)
        self.assertEqual(payload["max_tokens"], 2048)

    def test_post_can_require_a_repository_tool_call(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with (
            mock.patch("agent_factory.nvidia_builder.urllib.request.urlopen", return_value=response) as open_url,
            mock.patch("agent_factory.nvidia_builder.json.load", return_value={"choices": []}),
        ):
            _post("model", [], "key", 30, tool_choice="required")
        payload = json.loads(open_url.call_args.args[0].data)
        self.assertEqual(payload["tool_choice"], "required")

    def test_usage_stops_the_builder_at_configured_cost_limit(self) -> None:
        response = {
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0},
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
        }
        with mock.patch("agent_factory.nvidia_builder._post", return_value=response):
            with self.assertRaisesRegex(NvidiaBuilderError, "estimated cost limit"):
                run_openai_builder(
                    "task",
                    Path("."),
                    provider="minimax",
                    model="MiniMax-M2.7",
                    api_key="key",
                    max_requests=1,
                    timeout_seconds=60,
                    max_cost_usd=0.25,
                    input_cost_per_million=0.3,
                    output_cost_per_million=1.2,
                )

    def test_openrouter_uses_reported_cost_including_cache_charges(self) -> None:
        response = {
            "usage": {
                "prompt_tokens": 400_000,
                "completion_tokens": 0,
                "cost": 1.01,
                "prompt_tokens_details": {"cache_write_tokens": 400_000},
            },
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
        }
        with mock.patch("agent_factory.nvidia_builder._post", return_value=response):
            with self.assertRaisesRegex(
                NvidiaBuilderError, "provider-reported cost limit"
            ):
                run_openai_builder(
                    "task",
                    Path("."),
                    provider="openrouter",
                    model="openai/gpt",
                    api_key="key",
                    max_requests=1,
                    timeout_seconds=60,
                    max_cost_usd=1,
                    input_cost_per_million=2,
                    output_cost_per_million=10,
                )

    def test_openrouter_missing_reported_cost_fails_closed(self) -> None:
        response = {
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
        }
        with mock.patch("agent_factory.nvidia_builder._post", return_value=response):
            with self.assertRaisesRegex(NvidiaBuilderError, "numeric usage.cost"):
                run_openai_builder(
                    "task",
                    Path("."),
                    provider="openrouter",
                    model="openai/gpt",
                    api_key="key",
                    max_requests=1,
                    timeout_seconds=60,
                    max_cost_usd=1,
                    input_cost_per_million=2,
                    output_cost_per_million=10,
                )

    def test_openrouter_nonfinite_reported_cost_fails_closed(self) -> None:
        for invalid_cost in (float("nan"), float("inf"), True, "0.1", -0.1):
            with self.subTest(cost=invalid_cost):
                response = {
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 1,
                        "cost": invalid_cost,
                    },
                    "choices": [
                        {"message": {"role": "assistant", "content": "done"}}
                    ],
                }
                with mock.patch(
                    "agent_factory.nvidia_builder._post", return_value=response
                ):
                    with self.assertRaisesRegex(
                        NvidiaBuilderError, "numeric usage.cost"
                    ):
                        run_openai_builder(
                            "task",
                            Path("."),
                            provider="openrouter",
                            model="openai/gpt",
                            api_key="key",
                            max_requests=1,
                            timeout_seconds=60,
                            max_cost_usd=1,
                            input_cost_per_million=2,
                            output_cost_per_million=10,
                        )

    def test_openrouter_accumulates_reported_cost_across_tool_calls(self) -> None:
        responses = [
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 1,
                    "cost": 0.60,
                },
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
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
                ],
            },
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 1,
                    "cost": 0.41,
                },
                "choices": [
                    {"message": {"role": "assistant", "content": "done"}}
                ],
            },
        ]
        with mock.patch("agent_factory.nvidia_builder._post", side_effect=responses):
            with self.assertRaisesRegex(
                NvidiaBuilderError, "provider-reported cost limit"
            ):
                run_openai_builder(
                    "task",
                    Path("."),
                    provider="openrouter",
                    model="openai/gpt",
                    api_key="key",
                    max_requests=2,
                    timeout_seconds=60,
                    max_cost_usd=1,
                    input_cost_per_million=2,
                    output_cost_per_million=10,
                )

    def test_shared_budget_accumulates_cost_across_provider_attempts(self) -> None:
        budget = ModelCostBudget(1)
        primary = {
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "cost": 0.60},
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
        }
        with mock.patch("agent_factory.nvidia_builder._post", return_value=primary):
            with self.assertRaisesRegex(NvidiaBuilderError, "repository tools"):
                run_openai_builder(
                    "task",
                    Path("."),
                    provider="openrouter",
                    model="openai/gpt",
                    api_key="key",
                    max_requests=1,
                    timeout_seconds=60,
                    max_cost_usd=1,
                    input_cost_per_million=2,
                    output_cost_per_million=10,
                    cost_budget=budget,
                )
        fallback = {
            "usage": {"prompt_tokens": 410_000, "completion_tokens": 0},
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
        }
        with mock.patch("agent_factory.nvidia_builder._post", return_value=fallback):
            with self.assertRaisesRegex(NvidiaBuilderError, "estimated cost limit"):
                run_openai_builder(
                    "task",
                    Path("."),
                    provider="minimax",
                    model="MiniMax-M2.7",
                    api_key="key",
                    max_requests=1,
                    timeout_seconds=60,
                    max_cost_usd=1,
                    input_cost_per_million=1,
                    output_cost_per_million=1,
                    cost_budget=budget,
                )
        self.assertAlmostEqual(budget.spent_usd, 1.01)
        self.assertEqual(
            budget.kind_label, "mixed provider-reported and estimated"
        )

    def test_visual_revision_images_are_sent_with_the_initial_prompt(self) -> None:
        response = {
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.000012},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path":"candidate.txt","content":"ready"}',
                                },
                            }
                        ],
                    }
                }
            ],
        }
        histories: list[list[dict]] = []
        choices: list[str] = []

        def respond(_model, messages, *_args, **_kwargs):
            histories.append(json.loads(json.dumps(messages)))
            choices.append(_kwargs["tool_choice"])
            return response

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            with mock.patch("agent_factory.nvidia_builder._post", side_effect=respond):
                run_openai_builder(
                    "Compare the evidence.",
                    root,
                    provider="openrouter",
                    model="vision-model",
                    api_key="key",
                    max_requests=1,
                    timeout_seconds=60,
                    max_cost_usd=3,
                    input_cost_per_million=0.1,
                    output_cost_per_million=0.2,
                    image_urls=("https://github.com/acme/evidence/raw/sha/drag.png",),
                )
        content = histories[0][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Compare the evidence."})
        self.assertEqual(
            content[1],
            {
                "type": "image_url",
                "image_url": {"url": "https://github.com/acme/evidence/raw/sha/drag.png"},
            },
        )
        self.assertEqual(choices, ["required"])

    def test_prepared_dirty_baseline_requires_tools_until_agent_edit(self) -> None:
        for tracked in (False, True):
            with self.subTest(tracked=tracked), tempfile.TemporaryDirectory() as tmp:
                responses = [
                    {
                        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "cost": 0.000003},
                        "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                            {"id": "call-1", "function": {"name": "list_files", "arguments": '{"pattern":"*"}'}}
                        ]}}],
                    },
                    {
                        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "cost": 0.000003},
                        "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                            {"id": "call-2", "function": {"name": "write_file", "arguments": '{"path":"prepared.txt","content":"agent state"}'}}
                        ]}}],
                    },
                    {
                        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "cost": 0.000003},
                        "choices": [{"message": {"role": "assistant", "content": "done"}}],
                    },
                ]
                choices: list[str] = []

                def respond(*_args, **kwargs):
                    choices.append(kwargs["tool_choice"])
                    return responses.pop(0)

                root = Path(tmp)
                subprocess.run(["git", "init", "-q"], cwd=root, check=True)
                prepared = root / "prepared.txt"
                if tracked:
                    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
                    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
                    prepared.write_text("committed state")
                    subprocess.run(["git", "add", "prepared.txt"], cwd=root, check=True)
                    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
                prepared.write_text("prepared dirty state")
                with mock.patch("agent_factory.nvidia_builder._post", side_effect=respond):
                    run_openai_builder(
                        "task", root, provider="openrouter", model="model", api_key="key",
                        max_requests=3, timeout_seconds=60, max_cost_usd=3,
                        input_cost_per_million=0.1, output_cost_per_million=0.2,
                    )
                self.assertEqual(prepared.read_text(), "agent state")
                self.assertEqual(choices, ["required", "required", "auto"])

    def test_priced_provider_must_report_usage(self) -> None:
        response = {"choices": [{"message": {"role": "assistant", "content": "done"}}]}
        with mock.patch("agent_factory.nvidia_builder._post", return_value=response):
            with self.assertRaisesRegex(NvidiaBuilderError, "omitted token usage"):
                run_openai_builder(
                    "task",
                    Path("."),
                    provider="minimax",
                    model="MiniMax-M2.7",
                    api_key="key",
                    max_requests=1,
                    timeout_seconds=60,
                    max_cost_usd=3,
                    input_cost_per_million=0.3,
                    output_cost_per_million=1.2,
                )

    def test_request_boundary_publishes_an_existing_candidate(self) -> None:
        response = {
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path":"candidate.txt","content":"ready"}',
                                },
                            }
                        ],
                    }
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            with mock.patch("agent_factory.nvidia_builder._post", return_value=response):
                summary, tool_count, cost = run_openai_builder(
                    "task",
                    root,
                    provider="minimax",
                    model="MiniMax-M2.7",
                    api_key="key",
                    max_requests=1,
                    timeout_seconds=60,
                    max_cost_usd=3,
                    input_cost_per_million=0.3,
                    output_cost_per_million=1.2,
                )
        self.assertIn("reviewable repository candidate", summary)
        self.assertEqual(tool_count, 1)
        self.assertGreater(cost, 0)

    def test_minimax_preserves_reasoning_state_before_tool_result(self) -> None:
        assistant = {
            "role": "assistant",
            "content": "",
            "reasoning_details": [
                {"type": "reasoning.text", "text": "private reasoning"}
            ],
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"candidate.txt","content":"ready"}',
                    },
                }
            ],
        }
        responses = iter(
            [
                {
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                    "choices": [{"message": assistant}],
                },
                {
                    "usage": {"prompt_tokens": 200, "completion_tokens": 10},
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "<builder_summary>Done.</builder_summary>",
                            }
                        }
                    ],
                },
            ]
        )
        histories: list[list[dict]] = []

        def respond(_model, messages, *_args, **_kwargs):
            histories.append(json.loads(json.dumps(messages)))
            return next(responses)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            with mock.patch("agent_factory.nvidia_builder._post", side_effect=respond):
                summary, tool_count, _ = run_openai_builder(
                    "task",
                    root,
                    provider="minimax",
                    model="MiniMax-M2.7",
                    api_key="key",
                    max_requests=2,
                    timeout_seconds=60,
                    max_cost_usd=3,
                    input_cost_per_million=0.3,
                    output_cost_per_million=1.2,
                )
        self.assertEqual(summary, "<builder_summary>Done.</builder_summary>")
        self.assertEqual(tool_count, 1)
        self.assertEqual(histories[1][1], assistant)
        self.assertEqual(histories[1][2]["role"], "tool")
        self.assertEqual(histories[1][2]["tool_call_id"], "call-1")

    def test_read_only_completion_fails_so_fallback_can_run(self) -> None:
        inspect_response = {
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"pattern":"nothing","path":"."}',
                                },
                            }
                        ],
                    }
                }
            ],
        }
        final_response = {
            "usage": {"prompt_tokens": 200, "completion_tokens": 10},
            "choices": [
                {"message": {"role": "assistant", "content": "I only inspected it."}}
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            with mock.patch(
                "agent_factory.nvidia_builder._post",
                side_effect=[inspect_response, final_response],
            ):
                with self.assertRaisesRegex(NvidiaBuilderError, "without repository changes"):
                    run_openai_builder(
                        "task",
                        root,
                        provider="minimax",
                        model="MiniMax-M2.7",
                        api_key="key",
                        max_requests=2,
                        timeout_seconds=60,
                        max_cost_usd=3,
                        input_cost_per_million=0.3,
                        output_cost_per_million=1.2,
                    )


if __name__ == "__main__":
    unittest.main()
