from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_factory.nvidia_builder import NvidiaBuilderError, _execute_tool, _inside


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


if __name__ == "__main__":
    unittest.main()
