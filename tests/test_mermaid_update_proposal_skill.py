"""Unit tests for MermaidUpdateProposalSkill."""
from __future__ import annotations

import json
import unittest

from accruvia_harness.skills.mermaid_update_proposal import MermaidUpdateProposalSkill


_INPUTS = {
    "objective_title": "Clarify control flow",
    "objective_summary": "Operator wants the diagram to be unambiguous.",
    "intent_summary": "Make the path explicit.",
    "success_definition": "Diagram has explicit decision nodes.",
    "non_negotiables": [],
    "current_mermaid": "flowchart TD\nA-->B",
    "directive": "Add an explicit gate node",
    "anchor_label": "",
    "rewrite_requested": False,
    "recent_comments": [],
}


class MermaidUpdateProposalSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = MermaidUpdateProposalSkill()

    def test_name(self) -> None:
        self.assertEqual("mermaid_update_proposal", self.skill.name)

    def test_build_prompt_contains_directive(self) -> None:
        prompt = self.skill.build_prompt(_INPUTS)
        self.assertIn("Add an explicit gate node", prompt)
        self.assertIn("flowchart TD", prompt)

    def test_parse_response_handles_legacy_keys(self) -> None:
        response = json.dumps({"summary": "added gate", "content": "flowchart TD\nA-->G-->B"})
        parsed = self.skill.parse_response(response)
        self.assertEqual("flowchart TD\nA-->G-->B", parsed["proposed_content"])
        self.assertEqual("added gate", parsed["rationale"])

    def test_parse_response_handles_new_keys(self) -> None:
        response = json.dumps(
            {"proposed_content": "flowchart TD\nA-->B", "rationale": "no change"}
        )
        parsed = self.skill.parse_response(response)
        self.assertEqual("flowchart TD\nA-->B", parsed["proposed_content"])
        self.assertEqual("no change", parsed["rationale"])

    def test_validate_output_rejects_empty_content(self) -> None:
        ok, _ = self.skill.validate_output({"proposed_content": "", "rationale": "x"})
        self.assertFalse(ok)

    def test_validate_output_accepts_payload(self) -> None:
        ok, errors = self.skill.validate_output(
            {"proposed_content": "flowchart TD\nA-->B", "rationale": "ok"}
        )
        self.assertTrue(ok, errors)


if __name__ == "__main__":
    unittest.main()
