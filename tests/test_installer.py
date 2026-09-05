from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_factory.cli import install


class InstallerTests(unittest.TestCase):
    def test_install_is_non_destructive_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            first = install(root, factory_ref="v0.1.0", force=False)
            self.assertTrue(all(value == "written" for value in first.values()))
            config_path = root / ".agent-factory/config.json"
            edited = json.loads(config_path.read_text())
            edited["project"]["name"] = "custom"
            config_path.write_text(json.dumps(edited))

            second = install(root, factory_ref="v0.2.0", force=False)
            self.assertTrue(all(value == "preserved" for value in second.values()))
            self.assertEqual(json.loads(config_path.read_text())["project"]["name"], "custom")

    def test_force_updates_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            install(root, factory_ref="old", force=False)
            install(root, factory_ref="new", force=True)
            review = (root / ".github/workflows/agent-review.yml").read_text()
            self.assertIn("@new", review)
