from __future__ import annotations

import unittest
from pathlib import Path

from agent_factory.cli import default_config
from agent_factory.config import parse_config
from agent_factory.github_builder import BuilderBlocked
from agent_factory.github_figma_writer import (
    _validate_record_paths,
    build_prompt,
    format_issue_status,
    parse_result,
)
from agent_factory.protocol import decode_data


class FigmaWriterTests(unittest.TestCase):
    def test_ready_result_requires_file_and_node_urls(self) -> None:
        response = (
            '<figma_writer_result>{"status":"ready","summary":"Done",'
            '"file_url":"https://www.figma.com/design/file",'
            '"node_urls":["https://www.figma.com/design/file?node-id=1-2"]}'
            '</figma_writer_result>'
        )
        result = parse_result(response)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["node_urls"]), 1)

        with self.assertRaisesRegex(BuilderBlocked, "requires Figma"):
            parse_result(
                '<figma_writer_result>{"status":"ready","summary":"Done",'
                '"file_url":"","node_urls":[]}</figma_writer_result>'
            )

    def test_not_applicable_result_cannot_smuggle_urls(self) -> None:
        with self.assertRaisesRegex(BuilderBlocked, "cannot include"):
            parse_result(
                '<figma_writer_result>{"status":"not_applicable","summary":"No canvas",'
                '"file_url":"https://www.figma.com/design/file","node_urls":[]}'
                '</figma_writer_result>'
            )

    def test_delivery_record_boundary_rejects_source_changes(self) -> None:
        _validate_record_paths(("docs/contracts/screen.md", "REGISTRY.md"))
        with self.assertRaisesRegex(BuilderBlocked, "outside"):
            _validate_record_paths(("Sources/Screen.swift",))

    def test_prompt_assigns_canvas_only_and_requires_readback(self) -> None:
        raw = default_config("demo")
        raw["figma"].update({"enabled": True, "lease_key": "samuel-primary"})
        prompt = build_prompt(
            parse_config(raw),
            {"number": 12, "title": "Check-in", "body": "Deliver Figma"},
            {"number": 53, "title": "Check-in", "body": "Closes #12"},
            Path("."),
        )
        self.assertIn("own only the editable Figma delivery phase", prompt)
        self.assertIn("Read the resulting nodes back", prompt)
        self.assertIn("issue-12.md", prompt)

    def test_issue_status_binds_authenticated_role_to_exact_head(self) -> None:
        head = "a" * 40
        body = format_issue_status(
            "<!-- figma:test -->", "12", "delivered", "Done.",
            "https://github.com/o/r/pull/53", head=head,
        )
        data = decode_data(body)
        self.assertEqual(data["role"], "figma_writer")
        self.assertEqual(data["head"], head)


if __name__ == "__main__":
    unittest.main()
