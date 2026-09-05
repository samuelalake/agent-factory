from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_factory.config import ProjectConfig
from agent_factory.context import catalog_skills, discover_context, select_skills


class ContextDiscoveryTests(unittest.TestCase):
    def test_selects_role_relevant_repository_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "skill" / "review"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: review\ndescription: >-\n  Review pull requests\n  against evidence.\n---\nRules\n"
            )
            other = root / "skill" / "deploy"
            other.mkdir(parents=True)
            (other / "SKILL.md").write_text(
                "---\nname: deploy\ndescription: Publish a web release.\n---\nRules\n"
            )
            chosen = select_skills(catalog_skills(root, ("skill",)), "fix parser", role="review", limit=1)
            self.assertEqual([item.name for item in chosen], ["review"])

    def test_context_stays_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("local")
            project = ProjectConfig(
                name="x",
                context_files=("AGENTS.md", "../outside"),
                skill_dirs=("skill", "../skills"),
                max_skills=3,
            )
            documents = discover_context(root, project, "task", role="review")
            self.assertEqual([(item.path, item.content) for item in documents], [("AGENTS.md", "local")])
