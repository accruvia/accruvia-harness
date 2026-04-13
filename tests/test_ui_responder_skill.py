"""Unit tests for UIResponderSkill."""
from __future__ import annotations

import json
import unittest

from accruvia_harness.skills.ui_responder import UIResponderSkill


class UIResponderSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = UIResponderSkill()

    def test_name(self) -> None:
        self.assertEqual("ui_responder", self.skill.name)

    def test_build_prompt_includes_message(self) -> None:
        prompt = self.skill.build_prompt(
            {
                "operator_message": "What stage am I in?",
                "context_payload": {"mode": "investigation"},
            }
        )
        self.assertIn("What stage am I in?", prompt)
        self.assertIn("investigation", prompt)

    def test_parse_response_normalizes_recommended_action(self) -> None:
        response = json.dumps(
            {
                "reply": "You are in mermaid review.",
                "recommended_action": "garbage_action",
                "evidence_refs": ["mermaid"],
                "mode_shift": "investigation",
            }
        )
        parsed = self.skill.parse_response(response)
        self.assertEqual("You are in mermaid review.", parsed["reply"])
        self.assertEqual("none", parsed["recommended_action"])
        self.assertEqual("investigation", parsed["mode_shift"])
        self.assertEqual(["mermaid"], parsed["evidence_refs"])

    def test_validate_output_rejects_empty_reply(self) -> None:
        ok, _ = self.skill.validate_output(
            {
                "reply": "",
                "recommended_action": "none",
                "evidence_refs": [],
                "mode_shift": "none",
            }
        )
        self.assertFalse(ok)

    def test_validate_output_accepts_payload(self) -> None:
        ok, errors = self.skill.validate_output(
            {
                "reply": "ok",
                "recommended_action": "none",
                "evidence_refs": [],
                "mode_shift": "none",
            }
        )
        self.assertTrue(ok, errors)


if __name__ == "__main__":
    unittest.main()
