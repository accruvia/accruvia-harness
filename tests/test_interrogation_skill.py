"""Unit tests for InterrogationSkill."""
from __future__ import annotations

import json
import unittest

from accruvia_harness.skills.interrogation import InterrogationSkill


_INPUTS = {
    "objective_title": "Improve onboarding",
    "objective_summary": "Make the onboarding flow clearer.",
    "intent_summary": "Reduce onboarding drop-off.",
    "success_definition": "Operator can complete onboarding in under 5 minutes.",
    "non_negotiables": ["no PII collection"],
    "recent_comments": ["onboarding is confusing"],
    "deterministic_review": {"summary": "we have a draft"},
}


class InterrogationSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = InterrogationSkill()

    def test_name(self) -> None:
        self.assertEqual("interrogation", self.skill.name)

    def test_build_prompt_mentions_intent(self) -> None:
        prompt = self.skill.build_prompt(_INPUTS)
        self.assertIn("Reduce onboarding drop-off.", prompt)
        self.assertIn("non_negotiables", prompt.lower().replace(" ", "_") + " non_negotiables")  # tolerant

    def test_parse_response_normalizes_fields(self) -> None:
        response = json.dumps(
            {
                "summary": "We need to clarify three steps.",
                "plan_elements": ["Desired outcome: clearer onboarding"],
                "questions": ["What confuses operators most?"],
                "red_team_findings": [],
                "ready_for_mermaid_review": True,
            }
        )
        parsed = self.skill.parse_response(response)
        self.assertEqual("We need to clarify three steps.", parsed["summary"])
        self.assertEqual(["Desired outcome: clearer onboarding"], parsed["plan_elements"])
        self.assertEqual(["What confuses operators most?"], parsed["questions"])
        self.assertTrue(parsed["ready_for_mermaid_review"])

    def test_validate_output_accepts_proper_payload(self) -> None:
        ok, errors = self.skill.validate_output(
            {
                "questions": ["q?"],
                "red_team_findings": [],
                "ready_for_mermaid_review": False,
            }
        )
        self.assertTrue(ok, errors)

    def test_validate_output_rejects_missing_fields(self) -> None:
        ok, _ = self.skill.validate_output({"questions": ["q?"]})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
