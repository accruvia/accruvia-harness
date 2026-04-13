"""Unit tests for AtomicDecompositionSkill."""
from __future__ import annotations

import json
import unittest

from accruvia_harness.skills.atomic_decomposition import AtomicDecompositionSkill


_INPUTS = {
    "objective_title": "Decompose validation flow",
    "objective_summary": "Split validation into atomic units.",
    "intent_summary": "We need atomic, testable units.",
    "success_definition": "Each unit is one function with one test.",
    "non_negotiables": ["one function per unit"],
    "mermaid_content": "flowchart TD\nA-->B-->C",
    "repo_context": "src/accruvia_harness/validate.py",
    "recent_comments": ["please keep units small"],
}


class AtomicDecompositionSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = AtomicDecompositionSkill()

    def test_name(self) -> None:
        self.assertEqual("atomic_decomposition", self.skill.name)

    def test_build_prompt_includes_context(self) -> None:
        prompt = self.skill.build_prompt(_INPUTS)
        self.assertIn("ATOMIC", prompt)
        self.assertIn("Decompose validation flow", prompt)
        self.assertIn("src/accruvia_harness/validate.py", prompt)

    def test_parse_response_extracts_units(self) -> None:
        response = json.dumps(
            {
                "units": [
                    {
                        "title": "Add gate function",
                        "objective": "Add gate() in validate.py.",
                        "rationale": "Single function unit.",
                        "strategy": "atomic_from_mermaid",
                        "files_involved": ["src/accruvia_harness/validate.py"],
                    }
                ]
            }
        )
        parsed = self.skill.parse_response(response)
        self.assertEqual(1, len(parsed["units"]))
        unit = parsed["units"][0]
        self.assertEqual("Add gate function", unit["title"])
        self.assertEqual(["src/accruvia_harness/validate.py"], unit["files_involved"])

    def test_parse_response_handles_missing_units(self) -> None:
        parsed = self.skill.parse_response("not json")
        self.assertEqual({"units": []}, parsed)

    def test_validate_output_requires_non_empty_units(self) -> None:
        ok, errors = self.skill.validate_output({"units": []})
        self.assertFalse(ok)
        self.assertTrue(errors)

    def test_validate_output_requires_title_and_objective(self) -> None:
        ok, _ = self.skill.validate_output({"units": [{"title": ""}]})
        self.assertFalse(ok)

    def test_validate_output_accepts_proper_units(self) -> None:
        ok, errors = self.skill.validate_output(
            {
                "units": [
                    {"title": "do thing", "objective": "the thing"},
                ]
            }
        )
        self.assertTrue(ok, errors)


if __name__ == "__main__":
    unittest.main()
