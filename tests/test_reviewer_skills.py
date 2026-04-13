"""Unit tests for the per-dimension objective-review reviewer skills."""
from __future__ import annotations

import json
import unittest
from typing import Any

from accruvia_harness.skills.reviewers import REVIEWER_SKILLS
from accruvia_harness.skills.reviewers.atomic_fidelity import ReviewAtomicFidelitySkill
from accruvia_harness.skills.reviewers.code_structure import ReviewCodeStructureSkill
from accruvia_harness.skills.reviewers.devops import ReviewDevOpsSkill
from accruvia_harness.skills.reviewers.integration_e2e_coverage import ReviewIntegrationE2ESkill
from accruvia_harness.skills.reviewers.intent_fidelity import ReviewIntentFidelitySkill
from accruvia_harness.skills.reviewers.security import ReviewSecuritySkill
from accruvia_harness.skills.reviewers.unit_test_coverage import ReviewUnitTestCoverageSkill


_INPUTS: dict[str, Any] = {
    "objective_title": "Reduce regression rate",
    "objective_summary": "We keep regressing the validate path.",
    "intent_summary": "Validate must always run on every promotion.",
    "success_definition": "Zero regressions in the validate path for 30 days.",
    "non_negotiables": ["validate must run before promotion"],
    "mermaid_content": "flowchart TD\nA-->B",
    "task_titles": ["Add validation gate"],
    "changed_files": ["src/accruvia_harness/validate.py"],
    "diff_text": "+def gate(): ...",
}


class _ReviewerSkillContract:
    skill_cls: type
    expected_dimension: str
    expected_name: str

    def _build(self):
        return self.skill_cls()

    def test_name_and_dimension(self) -> None:
        skill = self._build()
        self.assertEqual(self.expected_name, skill.name)  # type: ignore[attr-defined]
        self.assertEqual(self.expected_dimension, skill.dimension)  # type: ignore[attr-defined]

    def test_schema_required_fields(self) -> None:
        skill = self._build()
        required = set(skill.output_schema.get("required") or [])  # type: ignore[attr-defined]
        self.assertEqual({"dimension", "verdict", "summary", "findings"}, required)

    def test_build_prompt_contains_dimension(self) -> None:
        skill = self._build()
        prompt = skill.build_prompt(_INPUTS)  # type: ignore[attr-defined]
        self.assertIn(f"Your assigned dimension is '{self.expected_dimension}'", prompt)
        self.assertIn(_INPUTS["objective_title"], prompt)
        self.assertIn(_INPUTS["mermaid_content"], prompt)

    def test_parse_response_extracts_json(self) -> None:
        skill = self._build()
        response = json.dumps(
            {
                "dimension": self.expected_dimension,
                "verdict": "concern",
                "summary": "Flagged a concern.",
                "findings": ["finding 1", "finding 2"],
            }
        )
        parsed = skill.parse_response(response)  # type: ignore[attr-defined]
        self.assertEqual(self.expected_dimension, parsed["dimension"])
        self.assertEqual("concern", parsed["verdict"])
        self.assertEqual(["finding 1", "finding 2"], parsed["findings"])

    def test_validate_output_accepts_correct_dimension(self) -> None:
        skill = self._build()
        ok, errors = skill.validate_output(  # type: ignore[attr-defined]
            {
                "dimension": self.expected_dimension,
                "verdict": "pass",
                "summary": "ok",
                "findings": [],
            }
        )
        self.assertTrue(ok, errors)

    def test_validate_output_rejects_wrong_dimension(self) -> None:
        skill = self._build()
        ok, errors = skill.validate_output(  # type: ignore[attr-defined]
            {
                "dimension": "not_my_dimension",
                "verdict": "pass",
                "summary": "ok",
                "findings": [],
            }
        )
        self.assertFalse(ok)
        self.assertTrue(errors)

    def test_validate_output_rejects_unknown_verdict(self) -> None:
        skill = self._build()
        ok, _ = skill.validate_output(  # type: ignore[attr-defined]
            {
                "dimension": self.expected_dimension,
                "verdict": "totally_invalid",
                "summary": "ok",
                "findings": [],
            }
        )
        self.assertFalse(ok)


class ReviewIntentFidelityTests(_ReviewerSkillContract, unittest.TestCase):
    skill_cls = ReviewIntentFidelitySkill
    expected_dimension = "intent_fidelity"
    expected_name = "review_intent_fidelity"


class ReviewUnitTestCoverageTests(_ReviewerSkillContract, unittest.TestCase):
    skill_cls = ReviewUnitTestCoverageSkill
    expected_dimension = "unit_test_coverage"
    expected_name = "review_unit_test_coverage"


class ReviewIntegrationE2ETests(_ReviewerSkillContract, unittest.TestCase):
    skill_cls = ReviewIntegrationE2ESkill
    expected_dimension = "integration_e2e_coverage"
    expected_name = "review_integration_e2e_coverage"


class ReviewSecurityTests(_ReviewerSkillContract, unittest.TestCase):
    skill_cls = ReviewSecuritySkill
    expected_dimension = "security"
    expected_name = "review_security"


class ReviewDevOpsTests(_ReviewerSkillContract, unittest.TestCase):
    skill_cls = ReviewDevOpsSkill
    expected_dimension = "devops"
    expected_name = "review_devops"


class ReviewAtomicFidelityTests(_ReviewerSkillContract, unittest.TestCase):
    skill_cls = ReviewAtomicFidelitySkill
    expected_dimension = "atomic_fidelity"
    expected_name = "review_atomic_fidelity"


class ReviewCodeStructureTests(_ReviewerSkillContract, unittest.TestCase):
    skill_cls = ReviewCodeStructureSkill
    expected_dimension = "code_structure"
    expected_name = "review_code_structure"


class ReviewerSkillsRegistryTests(unittest.TestCase):
    def test_all_seven_reviewer_skills_exposed(self) -> None:
        names = sorted(skill_cls().name for skill_cls in REVIEWER_SKILLS)
        self.assertEqual(
            sorted(
                [
                    "review_atomic_fidelity",
                    "review_code_structure",
                    "review_devops",
                    "review_integration_e2e_coverage",
                    "review_intent_fidelity",
                    "review_security",
                    "review_unit_test_coverage",
                ]
            ),
            names,
        )


if __name__ == "__main__":
    unittest.main()
