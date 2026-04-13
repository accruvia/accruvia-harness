"""Unit tests for CognitionHeartbeatSkill."""
from __future__ import annotations

import json
import unittest

from accruvia_harness.skills.cognition_heartbeat import CognitionHeartbeatSkill


class CognitionHeartbeatSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = CognitionHeartbeatSkill()

    def test_name(self) -> None:
        self.assertEqual("cognition_heartbeat", self.skill.name)

    def test_build_prompt_passthrough(self) -> None:
        self.assertEqual("hello", self.skill.build_prompt({"prompt": "hello"}))

    def test_build_prompt_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            self.skill.build_prompt({})

    def test_parse_response_extracts_fields(self) -> None:
        response = json.dumps(
            {
                "summary": "All clear.",
                "priority_focus": "ship next milestone",
                "issue_creation_needed": False,
                "proposed_tasks": [],
            }
        )
        parsed = self.skill.parse_response(response)
        self.assertEqual("All clear.", parsed["summary"])
        self.assertFalse(parsed["issue_creation_needed"])
        self.assertEqual([], parsed["proposed_tasks"])

    def test_validate_output_requires_required_fields(self) -> None:
        ok, _ = self.skill.validate_output({"summary": "x"})
        self.assertFalse(ok)
        ok, errors = self.skill.validate_output(
            {
                "summary": "x",
                "issue_creation_needed": False,
                "proposed_tasks": [],
            }
        )
        self.assertTrue(ok, errors)


if __name__ == "__main__":
    unittest.main()
