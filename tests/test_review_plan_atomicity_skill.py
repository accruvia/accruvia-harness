"""Unit tests for ReviewPlanAtomicitySkill."""
from __future__ import annotations

import json
import unittest

from accruvia_harness.skills.review_plan_atomicity import ReviewPlanAtomicitySkill


def _sample_plans() -> list[dict]:
    return [
        {
            "local_id": "p1",
            "label": "Add Plan.summary() method",
            "depends_on": [],
            "target_impl": "src/accruvia_harness/domain.py::Plan.summary",
            "target_test": "tests/test_domain.py::test_plan_summary",
            "transformation": "Return formatted id/objective/status string",
            "input_samples": [{"id": "plan_abc", "status": "approved"}],
            "output_samples": ["plan_abc (approved)"],
        },
        {
            "local_id": "p2",
            "label": "Wire Plan.summary() into bench view",
            "depends_on": ["p1"],
            "target_impl": "bin/accruvia-objective-bench",
            "target_test": "tests/test_bench.py::test_bench_shows_plan_summary",
            "transformation": "Call plan.summary() in the objective detail view",
            "input_samples": [{"plan_count": 3}],
            "output_samples": ["3 plan summaries printed"],
        },
    ]


def _sample_inputs() -> dict:
    return {
        "objective_title": "Add Plan.summary() method",
        "objective_summary": "Human-readable repr for Plan rows",
        "intent_summary": "Developers debugging plan linkage need a short repr",
        "success_definition": "plan.summary() returns a one-line string",
        "non_negotiables": ["No changes outside domain.py"],
        "generator_output": {"plans": _sample_plans()},
    }


class ReviewPlanAtomicityPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = ReviewPlanAtomicitySkill()

    def test_prompt_includes_plan_list_and_intent(self):
        prompt = self.skill.build_prompt(_sample_inputs())
        # Objective and non-negotiables rendered
        self.assertIn("Add Plan.summary() method", prompt)
        self.assertIn("No changes outside domain.py", prompt)
        # The plan list is included as JSON
        self.assertIn("p1", prompt)
        self.assertIn("p2", prompt)
        self.assertIn("target_impl", prompt)
        self.assertIn("transformation", prompt)
        # Rule instructions present
        self.assertIn("CROSS-PLAN FILE SPRAWL", prompt)
        self.assertIn("NON-NEGOTIABLE COMPLETENESS", prompt)

    def test_prompt_truncates_huge_plan_list(self):
        # Build a giant plans payload to exercise the 12k char truncation
        inputs = _sample_inputs()
        huge_plan = {
            "local_id": "pN",
            "label": "x",
            "depends_on": [],
            "target_impl": "src/foo.py",
            "target_test": "tests/test_foo.py",
            "transformation": "x" * 5000,
            "input_samples": [{}],
            "output_samples": [{}],
        }
        inputs["generator_output"] = {"plans": [huge_plan] * 5}
        prompt = self.skill.build_prompt(inputs)
        self.assertIn("truncated for prompt budget", prompt)


class ReviewPlanAtomicityParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = ReviewPlanAtomicitySkill()

    def test_parses_pass_verdict(self):
        text = json.dumps(
            {
                "dimension": "plan_atomicity",
                "verdict": "pass",
                "summary": "Decomposition is atomic and coherent.",
                "findings": [],
            }
        )
        parsed = self.skill.parse_response(text)
        self.assertEqual("pass", parsed["verdict"])
        self.assertEqual([], parsed["findings"])

    def test_parses_concern_with_findings(self):
        text = json.dumps(
            {
                "verdict": "concern",
                "summary": "Two plans touch domain.py without a dependency.",
                "findings": [
                    {
                        "plan_local_id": "p3",
                        "severity": "major",
                        "class": "sprawl",
                        "finding": "p3 and p5 both modify src/foo.py",
                        "suggested_fix": "Make p5 depend on p3",
                    }
                ],
            }
        )
        parsed = self.skill.parse_response(text)
        self.assertEqual("concern", parsed["verdict"])
        self.assertEqual(1, len(parsed["findings"]))
        self.assertEqual("major", parsed["findings"][0]["severity"])
        self.assertEqual("p3", parsed["findings"][0]["plan_local_id"])

    def test_normalizes_unknown_severity_to_minor(self):
        text = json.dumps(
            {
                "verdict": "pass",
                "summary": "ok",
                "findings": [
                    {
                        "plan_local_id": "p1",
                        "severity": "trivial",  # not in allowed set
                        "finding": "tiny thing",
                    }
                ],
            }
        )
        parsed = self.skill.parse_response(text)
        self.assertEqual("minor", parsed["findings"][0]["severity"])

    def test_drops_findings_with_empty_text(self):
        text = json.dumps(
            {
                "verdict": "pass",
                "summary": "ok",
                "findings": [
                    {"plan_local_id": "p1", "severity": "minor", "finding": ""},
                    {"plan_local_id": "p1", "severity": "minor", "finding": "real issue"},
                ],
            }
        )
        parsed = self.skill.parse_response(text)
        self.assertEqual(1, len(parsed["findings"]))

    def test_non_json_returns_empty(self):
        self.assertEqual({}, self.skill.parse_response("not json at all"))


class ReviewPlanAtomicityValidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = ReviewPlanAtomicitySkill()

    def test_valid_pass_verdict_validates(self):
        parsed = {
            "verdict": "pass",
            "summary": "Decomposition is atomic.",
            "findings": [],
        }
        ok, errors = self.skill.validate_output(parsed)
        self.assertTrue(ok, msg=errors)

    def test_valid_pass_with_minor_findings_validates(self):
        parsed = {
            "verdict": "pass",
            "summary": "Decomposition is atomic with minor notes.",
            "findings": [
                {
                    "plan_local_id": "p1",
                    "severity": "minor",
                    "finding": "Could add another sample",
                }
            ],
        }
        ok, errors = self.skill.validate_output(parsed)
        self.assertTrue(ok, msg=errors)

    def test_pass_with_major_finding_rejected(self):
        parsed = {
            "verdict": "pass",
            "summary": "Decomposition is atomic.",
            "findings": [
                {
                    "plan_local_id": "p1",
                    "severity": "major",
                    "finding": "Real sprawl issue",
                }
            ],
        }
        ok, errors = self.skill.validate_output(parsed)
        self.assertFalse(ok)
        self.assertIn("pass is inconsistent", errors[0])

    def test_concern_without_major_finding_rejected(self):
        parsed = {
            "verdict": "concern",
            "summary": "Minor issues only, but I picked concern anyway.",
            "findings": [
                {
                    "plan_local_id": "p1",
                    "severity": "minor",
                    "finding": "minor thing",
                }
            ],
        }
        ok, errors = self.skill.validate_output(parsed)
        self.assertFalse(ok)
        self.assertIn("concern requires at least one major", errors[0])

    def test_remediation_required_without_critical_rejected(self):
        parsed = {
            "verdict": "remediation_required",
            "summary": "Blocking issue",
            "findings": [
                {
                    "plan_local_id": "p1",
                    "severity": "major",
                    "finding": "not critical",
                }
            ],
        }
        ok, errors = self.skill.validate_output(parsed)
        self.assertFalse(ok)
        self.assertIn("remediation_required requires at least one critical", errors[0])

    def test_remediation_required_with_critical_validates(self):
        parsed = {
            "verdict": "remediation_required",
            "summary": "Critical gap",
            "findings": [
                {
                    "plan_local_id": "p1",
                    "severity": "critical",
                    "finding": "Non-negotiable X is not addressed",
                }
            ],
        }
        ok, errors = self.skill.validate_output(parsed)
        self.assertTrue(ok, msg=errors)

    def test_empty_summary_rejected(self):
        parsed = {"verdict": "pass", "summary": "", "findings": []}
        ok, errors = self.skill.validate_output(parsed)
        self.assertFalse(ok)
        self.assertIn("summary", errors[0])

    def test_invalid_verdict_rejected(self):
        parsed = {"verdict": "maybe", "summary": "x", "findings": []}
        ok, errors = self.skill.validate_output(parsed)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
