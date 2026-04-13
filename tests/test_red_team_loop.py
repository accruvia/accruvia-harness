"""Unit tests for RedTeamLoopOrchestrator."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from accruvia_harness.services.red_team_loop import (
    RedTeamLoopOrchestrator,
    default_findings_extractor,
)
from accruvia_harness.skills.base import SkillResult


class _StubSkill:
    def __init__(self, name: str) -> None:
        self.name = name


class _StubRegistry:
    def __init__(self, skills: dict[str, Any]) -> None:
        self._skills = skills

    def get(self, name: str) -> Any:
        return self._skills[name]


class _StubLLMRouter:
    pass


class _StubStore:
    pass


class RedTeamLoopOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.workspace_root = Path(self.tmpdir.name)
        self.generator = _StubSkill("generator")
        self.reviewer = _StubSkill("reviewer")
        self.registry = _StubRegistry({
            "generator": self.generator,
            "reviewer": self.reviewer,
        })
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _patch_invoke(self, generator_results: list[SkillResult], reviewer_results: list[SkillResult] | None = None):
        from accruvia_harness.services import red_team_loop as mod

        gen_iter = iter(generator_results)
        rev_iter = iter(reviewer_results or [])

        def _fake_invoke(skill, invocation, llm_router, telemetry=None):
            self.calls.append((skill.name, dict(invocation.inputs)))
            if skill.name == "generator":
                return next(gen_iter)
            return next(rev_iter)

        self._original = mod.invoke_skill
        mod.invoke_skill = _fake_invoke

    def _restore(self):
        from accruvia_harness.services import red_team_loop as mod
        mod.invoke_skill = self._original

    def _make_orchestrator(self):
        return RedTeamLoopOrchestrator(
            skill_registry=self.registry,
            llm_router=_StubLLMRouter(),
            store=_StubStore(),
            workspace_root=self.workspace_root,
        )

    def test_stops_when_predicate_satisfied_on_first_round(self) -> None:
        self._patch_invoke([
            SkillResult(skill_name="generator", success=True, output={"value": "good", "red_team_findings": []}),
        ])
        try:
            result = self._make_orchestrator().execute(
                generator_skill_name="generator",
                reviewer_skill_names=None,
                initial_inputs={"seed": 1},
                stopping_predicate=lambda output, reviewer_results, round_number: True,
                max_rounds=5,
                project_id="proj-1",
                loop_label="test",
                loop_key="k1",
            )
        finally:
            self._restore()
        self.assertTrue(result.success)
        self.assertEqual(1, result.rounds_completed)
        self.assertEqual("predicate_satisfied", result.stop_reason)
        self.assertEqual("good", result.final_output["value"])
        self.assertEqual(1, len(self.calls))

    def test_retries_and_threads_prior_findings_into_next_round(self) -> None:
        self._patch_invoke([
            SkillResult(
                skill_name="generator",
                success=True,
                output={"value": "bad", "red_team_findings": ["missing X", "unclear Y"]},
            ),
            SkillResult(
                skill_name="generator",
                success=True,
                output={"value": "good", "red_team_findings": []},
            ),
        ])
        stop_calls: list[int] = []

        def stopping(output, reviewer_results, round_number):
            stop_calls.append(round_number)
            return not list(output.get("red_team_findings") or [])

        try:
            result = self._make_orchestrator().execute(
                generator_skill_name="generator",
                reviewer_skill_names=None,
                initial_inputs={"seed": 1},
                stopping_predicate=stopping,
                max_rounds=5,
                project_id="proj-1",
                loop_label="test",
                loop_key="k2",
            )
        finally:
            self._restore()
        self.assertTrue(result.success)
        self.assertEqual(2, result.rounds_completed)
        self.assertEqual("predicate_satisfied", result.stop_reason)
        self.assertEqual([1, 2], stop_calls)
        # Round 2 invocation must have received prior_round_findings from round 1.
        round2_inputs = self.calls[1][1]
        self.assertEqual(2, round2_inputs["round_number"])
        self.assertEqual(["missing X", "unclear Y"], round2_inputs["prior_round_findings"])
        # Round 1 should have empty prior findings by default.
        round1_inputs = self.calls[0][1]
        self.assertEqual([], round1_inputs.get("prior_round_findings"))

    def test_hits_max_rounds_without_predicate_success(self) -> None:
        results = [
            SkillResult(
                skill_name="generator",
                success=True,
                output={"value": f"try{i}", "red_team_findings": [f"still failing {i}"]},
            )
            for i in range(3)
        ]
        self._patch_invoke(results)
        try:
            result = self._make_orchestrator().execute(
                generator_skill_name="generator",
                reviewer_skill_names=None,
                initial_inputs={"seed": 1},
                stopping_predicate=lambda output, reviewer_results, round_number: False,
                max_rounds=3,
                project_id="proj-1",
                loop_label="test",
                loop_key="k3",
            )
        finally:
            self._restore()
        self.assertEqual(3, result.rounds_completed)
        self.assertEqual("max_rounds_exhausted", result.stop_reason)
        self.assertTrue(result.success)  # generator itself succeeded each round
        self.assertEqual("try2", result.final_output["value"])

    def test_generator_failure_retries_then_succeeds(self) -> None:
        """A failed generator round feeds its errors back into prior_round_findings
        and the loop tries again — this is the restored retry-on-validation-failure
        behaviour that the old RedTeamLoopService used to provide."""
        self._patch_invoke([
            SkillResult(skill_name="generator", success=False, errors=["parse_failed: missing key"]),
            SkillResult(skill_name="generator", success=True, output={"value": "recovered"}),
        ])
        try:
            result = self._make_orchestrator().execute(
                generator_skill_name="generator",
                reviewer_skill_names=None,
                initial_inputs={},
                stopping_predicate=lambda *_: True,
                max_rounds=5,
                project_id="proj-1",
                loop_label="test",
                loop_key="k4",
            )
        finally:
            self._restore()
        self.assertTrue(result.success)
        self.assertEqual(2, result.rounds_completed)
        self.assertEqual("predicate_satisfied", result.stop_reason)
        round2_inputs = self.calls[1][1]
        self.assertIn("generator_failed", round2_inputs["prior_round_findings"][0])
        self.assertIn("parse_failed", round2_inputs["prior_round_findings"][0])

    def test_generator_failure_halts_on_max_rounds(self) -> None:
        self._patch_invoke([
            SkillResult(skill_name="generator", success=False, errors=["parse_failed"]),
            SkillResult(skill_name="generator", success=False, errors=["parse_failed"]),
        ])
        try:
            result = self._make_orchestrator().execute(
                generator_skill_name="generator",
                reviewer_skill_names=None,
                initial_inputs={},
                stopping_predicate=lambda *_: False,
                max_rounds=2,
                project_id="proj-1",
                loop_label="test",
                loop_key="k4b",
            )
        finally:
            self._restore()
        self.assertFalse(result.success)
        self.assertEqual("generator_failed", result.stop_reason)
        self.assertEqual(2, result.rounds_completed)

    def test_reviewer_findings_feed_into_next_round(self) -> None:
        self._patch_invoke(
            [
                SkillResult(skill_name="generator", success=True, output={"units": [{"title": "draft1"}]}),
                SkillResult(skill_name="generator", success=True, output={"units": [{"title": "final"}]}),
            ],
            [
                SkillResult(
                    skill_name="reviewer",
                    success=True,
                    output={"verdict": "concern", "findings": ["unit 1 is too broad"]},
                ),
                SkillResult(
                    skill_name="reviewer",
                    success=True,
                    output={"verdict": "pass", "findings": []},
                ),
            ],
        )

        def stopping(output, reviewer_results, round_number):
            rev = reviewer_results.get("reviewer")
            return bool(rev and rev.success and rev.output.get("verdict") == "pass")

        try:
            result = self._make_orchestrator().execute(
                generator_skill_name="generator",
                reviewer_skill_names=["reviewer"],
                initial_inputs={"seed": 1},
                stopping_predicate=stopping,
                max_rounds=5,
                project_id="proj-1",
                loop_label="test",
                loop_key="k5",
            )
        finally:
            self._restore()
        self.assertTrue(result.success)
        self.assertEqual(2, result.rounds_completed)
        # Ordered calls: gen r1, reviewer r1, gen r2, reviewer r2
        self.assertEqual(
            ["generator", "reviewer", "generator", "reviewer"],
            [name for name, _ in self.calls],
        )
        # Round 2 generator inputs must carry prior findings prefixed by reviewer name.
        r2_inputs = self.calls[2][1]
        prior = r2_inputs["prior_round_findings"]
        self.assertEqual(1, len(prior))
        self.assertIn("unit 1 is too broad", prior[0])
        self.assertIn("reviewer", prior[0])

    def test_default_findings_extractor_aggregates_sources(self) -> None:
        generator_output = {"red_team_findings": ["self-concern A"]}
        reviewer_results = {
            "a_reviewer": SkillResult(
                skill_name="a_reviewer",
                success=True,
                output={"findings": ["b-concern"]},
            ),
            "b_reviewer": SkillResult(
                skill_name="b_reviewer",
                success=False,
                errors=["timeout"],
            ),
        }
        findings = default_findings_extractor(generator_output, reviewer_results)
        self.assertEqual(3, len(findings))
        self.assertEqual("self-concern A", findings[0])
        self.assertIn("a_reviewer", findings[1])
        self.assertIn("b-concern", findings[1])
        self.assertIn("b_reviewer", findings[2])
        self.assertIn("timeout", findings[2])


if __name__ == "__main__":
    unittest.main()
