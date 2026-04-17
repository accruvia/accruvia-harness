"""Tests for typed domain classes: ReviewPacket, PlanSlice, InterrogationReview."""
from __future__ import annotations

import unittest

from accruvia_harness.domain import (
    ArtifactSchema,
    EvidenceContract,
    InterrogationReview,
    OrphanStrategy,
    PlanComplexity,
    PlanSlice,
    ReviewDimension,
    ReviewPacket,
    ReviewProgressStatus,
    ReviewSeverity,
    ReviewVerdict,
    _safe_enum,
)


class SafeEnumTests(unittest.TestCase):
    def test_valid_value(self) -> None:
        self.assertEqual(ReviewVerdict.PASS, _safe_enum(ReviewVerdict, "pass"))

    def test_invalid_value_returns_default(self) -> None:
        self.assertEqual(ReviewVerdict.CONCERN, _safe_enum(ReviewVerdict, "garbage", ReviewVerdict.CONCERN))

    def test_none_returns_default(self) -> None:
        self.assertIsNone(_safe_enum(ReviewVerdict, None))

    def test_empty_string_returns_default(self) -> None:
        self.assertIsNone(_safe_enum(ReviewVerdict, ""))

    def test_case_insensitive(self) -> None:
        self.assertEqual(ReviewVerdict.PASS, _safe_enum(ReviewVerdict, "PASS"))


class ReviewPacketRoundTripTests(unittest.TestCase):
    def test_empty_dict(self) -> None:
        p = ReviewPacket.from_dict({})
        self.assertEqual(ReviewVerdict.CONCERN, p.verdict)
        self.assertEqual("", p.reviewer)
        d = p.to_dict()
        self.assertEqual("concern", d["verdict"])

    def test_full_round_trip(self) -> None:
        raw = {
            "reviewer": "Intent agent",
            "dimension": "intent_fidelity",
            "verdict": "pass",
            "progress_status": "not_applicable",
            "severity": "",
            "owner_scope": "",
            "summary": "Intent aligns with objective",
            "findings": [],
            "evidence": ["64 completed tasks"],
            "required_artifact_type": "",
            "artifact_schema": {},
            "evidence_contract": {},
            "closure_criteria": "",
            "evidence_required": "",
            "repeat_reason": "",
            "llm_usage": {"cost_usd": 0.01},
            "llm_usage_reported": True,
            "llm_usage_source": "diagnostics",
            "backend": "skills_orchestrator",
            "prompt_path": "/tmp/prompt.txt",
            "response_path": "/tmp/response.md",
            "review_task_id": "task_123",
            "review_run_id": "run_456",
            "packet_record_id": "ctx_789",
        }
        p = ReviewPacket.from_dict(raw)
        self.assertEqual(ReviewVerdict.PASS, p.verdict)
        self.assertEqual("Intent agent", p.reviewer)
        self.assertEqual("intent_fidelity", p.dimension)
        d = p.to_dict()
        self.assertEqual(raw, d)

    def test_from_dict_none(self) -> None:
        p = ReviewPacket.from_dict(None)
        self.assertEqual("", p.reviewer)

    def test_invalid_verdict_defaults(self) -> None:
        p = ReviewPacket.from_dict({"verdict": "invalid_verdict"})
        self.assertEqual(ReviewVerdict.CONCERN, p.verdict)


class PlanSliceRoundTripTests(unittest.TestCase):
    def test_base_plan(self) -> None:
        raw = {"label": "Add feature", "dependencies": [], "derived_from": "plan_draft", "local_id": "p1"}
        s = PlanSlice.from_dict(raw)
        self.assertEqual("Add feature", s.label)
        self.assertEqual(PlanComplexity.MEDIUM, s.estimated_complexity)
        d = s.to_dict()
        self.assertEqual(raw, d)

    def test_trio_plan(self) -> None:
        raw = {
            "label": "Add bar method",
            "dependencies": ["plan_abc"],
            "derived_from": "plan_draft_trio",
            "local_id": "p2",
            "target_impl": "src/foo.py::Foo.bar",
            "target_test": "tests/test_foo.py::test_bar",
            "transformation": "Add bar to Foo",
            "input_samples": [1, 2],
            "output_samples": ["a", "b"],
            "resources": [],
            "supersedes": ["src/foo.py::Foo.old_bar"],
            "orphan_strategy": "absorb",
            "orphan_acceptance_reason": "",
            "risks": ["breaks callers"],
            "estimated_complexity": "small",
            "creates_new_file": True,
        }
        s = PlanSlice.from_dict(raw)
        self.assertEqual("src/foo.py::Foo.bar", s.target_impl)
        self.assertEqual(OrphanStrategy.ABSORB, s.orphan_strategy)
        self.assertEqual(PlanComplexity.SMALL, s.estimated_complexity)
        self.assertTrue(s.creates_new_file)
        d = s.to_dict()
        # to_dict omits empty lists/strings for optional TRIO fields
        self.assertEqual("Add bar method", d["label"])
        self.assertEqual("src/foo.py::Foo.bar", d["target_impl"])
        self.assertEqual("absorb", d["orphan_strategy"])
        self.assertEqual("small", d["estimated_complexity"])
        self.assertNotIn("resources", d)  # empty list omitted
        self.assertNotIn("orphan_acceptance_reason", d)  # empty string omitted

    def test_empty_dict(self) -> None:
        s = PlanSlice.from_dict({})
        self.assertEqual("", s.label)

    def test_invalid_complexity_defaults(self) -> None:
        s = PlanSlice.from_dict({"label": "x", "estimated_complexity": "unknown"})
        self.assertEqual(PlanComplexity.MEDIUM, s.estimated_complexity)


class InterrogationReviewRoundTripTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        raw = {
            "completed": True,
            "summary": "Intent is clear",
            "plan_elements": ["Step 1", "Step 2"],
            "questions": ["Q1"],
            "generated_by": "deterministic",
        }
        r = InterrogationReview.from_dict(raw)
        self.assertTrue(r.completed)
        self.assertEqual("deterministic", r.generated_by)
        self.assertIsNone(r.backend)
        d = r.to_dict()
        self.assertEqual(raw, d)

    def test_llm_generated(self) -> None:
        raw = {
            "completed": True,
            "summary": "Reviewed",
            "plan_elements": [],
            "questions": [],
            "generated_by": "llm",
            "backend": "claude",
            "prompt_path": "/tmp/p.txt",
            "response_path": "/tmp/r.md",
            "red_team_rounds": 3,
            "red_team_stop_reason": "predicate_satisfied",
        }
        r = InterrogationReview.from_dict(raw)
        self.assertEqual("claude", r.backend)
        self.assertEqual(3, r.red_team_rounds)
        d = r.to_dict()
        self.assertEqual(raw, d)

    def test_empty(self) -> None:
        r = InterrogationReview.from_dict({})
        self.assertFalse(r.completed)
        self.assertEqual("deterministic", r.generated_by)


class ArtifactSchemaTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        raw = {"type": "review_artifact", "description": "QA packet", "required_fields": ["review_id"]}
        s = ArtifactSchema.from_dict(raw)
        self.assertEqual("review_artifact", s.type)
        self.assertEqual(raw, s.to_dict())

    def test_none(self) -> None:
        s = ArtifactSchema.from_dict(None)
        self.assertEqual("", s.type)


class EvidenceContractTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        raw = {
            "required_artifact_type": "report",
            "artifact_schema": {"type": "report", "description": "d", "required_fields": ["id"]},
            "closure_criteria": "Must pass",
            "evidence_required": "Test evidence",
        }
        c = EvidenceContract.from_dict(raw)
        self.assertEqual("report", c.required_artifact_type)
        self.assertIsNotNone(c.artifact_schema)
        self.assertEqual("report", c.artifact_schema.type)
        self.assertEqual(raw, c.to_dict())


class EnumCoverageTests(unittest.TestCase):
    def test_review_dimensions_count(self) -> None:
        self.assertEqual(7, len(ReviewDimension))

    def test_plan_complexity_values(self) -> None:
        self.assertIn("trivial", [c.value for c in PlanComplexity])
        self.assertIn("too_large", [c.value for c in PlanComplexity])

    def test_orphan_strategy_values(self) -> None:
        self.assertEqual(3, len(OrphanStrategy))


if __name__ == "__main__":
    unittest.main()
