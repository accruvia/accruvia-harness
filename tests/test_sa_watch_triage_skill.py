"""Unit tests for SAWatchTriageSkill."""
from __future__ import annotations

import unittest

from accruvia_harness.skills.sa_watch_triage import SAWatchTriageSkill


class SAWatchTriageSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = SAWatchTriageSkill()

    def test_name(self) -> None:
        self.assertEqual("sa_watch_triage", self.skill.name)

    def test_build_prompt_passthrough(self) -> None:
        self.assertEqual("triage", self.skill.build_prompt({"prompt": "triage"}))

    def test_build_prompt_requires_prompt(self) -> None:
        with self.assertRaises(ValueError):
            self.skill.build_prompt({})

    def test_parse_response_strips(self) -> None:
        parsed = self.skill.parse_response("\n  recovery report  \n")
        self.assertEqual("recovery report", parsed["report"])

    def test_validate_output_rejects_empty(self) -> None:
        ok, _ = self.skill.validate_output({"report": ""})
        self.assertFalse(ok)

    def test_validate_output_accepts_text(self) -> None:
        ok, errors = self.skill.validate_output({"report": "ok"})
        self.assertTrue(ok, errors)


if __name__ == "__main__":
    unittest.main()
