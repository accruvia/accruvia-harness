"""Discipline test for the skills manager.

Every production LLM call must flow through ``skills/base.py:invoke_skill``.
The only legitimate direct ``llm_router.execute`` call site is inside
``invoke_skill`` itself. Discovered during the skills migration that
collapsed seven inline LLM call paths into the skill registry.
"""
from __future__ import annotations

import subprocess
import unittest


class SkillsManagerDisciplineTests(unittest.TestCase):
    def test_no_production_code_calls_llm_router_execute_directly(self) -> None:
        """Every production LLM call must go through skills/base.py:invoke_skill."""
        result = subprocess.run(
            [
                "grep",
                "-rn",
                "llm_router.execute",
                "src/accruvia_harness/",
                "--include=*.py",
            ],
            capture_output=True,
            text=True,
        )
        hits = [
            line
            for line in result.stdout.splitlines()
            if "skills/base.py" not in line and "__pycache__" not in line
        ]
        self.assertEqual(
            [],
            hits,
            f"Found direct llm_router.execute calls outside the manager: {hits}",
        )


if __name__ == "__main__":
    unittest.main()
