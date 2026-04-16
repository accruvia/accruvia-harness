"""Unit tests for services.trio_plan_orchestrator.generate_trio_plans.

The orchestrator glues three already-tested pieces together
(PlanDraftTrioSkill, ReviewPlanAtomicitySkill, RedTeamLoopOrchestrator),
so these tests patch those pieces out and verify the glue: the stopping
predicate, the TrioPlanningResult mapping, and the reviewer_verdict
extraction from loop history.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from accruvia_harness.services import trio_plan_orchestrator as mod
from accruvia_harness.services.red_team_loop import RedTeamLoopResult, RedTeamRound
from accruvia_harness.skills.base import SkillResult


class _StubSkillContext:
    pass


class _StubSkill:
    def __init__(self, *args, **kwargs) -> None:
        self.name = kwargs.get("_name", "stub")


class _StubRegistry:
    def __init__(self) -> None:
        self.registered: list[Any] = []

    def register(self, skill: Any) -> None:
        self.registered.append(skill)


class _StubOrchestrator:
    last_instance: "_StubOrchestrator | None" = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.execute_calls: list[dict[str, Any]] = []
        self._result_factory: Any = None
        _StubOrchestrator.last_instance = self

    def execute(self, **kwargs) -> RedTeamLoopResult:
        self.execute_calls.append(kwargs)
        return self._result_factory(kwargs)


def _round(
    round_number: int,
    gen_output: dict[str, Any],
    reviewer_verdict: str | None = None,
    reviewer_success: bool = True,
) -> RedTeamRound:
    gen = SkillResult(skill_name="plan_draft_trio", success=True, output=gen_output)
    reviewers: dict[str, SkillResult] = {}
    if reviewer_verdict is not None:
        reviewers["review_plan_atomicity"] = SkillResult(
            skill_name="review_plan_atomicity",
            success=reviewer_success,
            output={"verdict": reviewer_verdict, "findings": []},
            errors=[] if reviewer_success else ["boom"],
        )
    return RedTeamRound(
        round_number=round_number,
        generator_result=gen,
        reviewer_results=reviewers,
    )


class GenerateTrioPlansTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.workspace_root = Path(self.tmpdir.name)
        self._saved = {
            "PlanDraftTrioSkill": mod.PlanDraftTrioSkill,
            "ReviewPlanAtomicitySkill": mod.ReviewPlanAtomicitySkill,
            "SkillRegistry": mod.SkillRegistry,
            "RedTeamLoopOrchestrator": mod.RedTeamLoopOrchestrator,
        }
        mod.PlanDraftTrioSkill = lambda context: _StubSkill(_name="plan_draft_trio")
        mod.ReviewPlanAtomicitySkill = lambda: _StubSkill(_name="review_plan_atomicity")
        mod.SkillRegistry = _StubRegistry
        mod.RedTeamLoopOrchestrator = _StubOrchestrator

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(mod, name, value)
        self.tmpdir.cleanup()

    def _run(self, result_factory) -> mod.TrioPlanningResult:
        _StubOrchestrator.last_instance = None
        # Patch orchestrator construction to capture and install factory.
        original = mod.RedTeamLoopOrchestrator

        def _factory(**kwargs):
            inst = original(**kwargs)
            inst._result_factory = result_factory
            return inst

        mod.RedTeamLoopOrchestrator = _factory
        try:
            return mod.generate_trio_plans(
                intent_inputs={"objective_title": "t", "non_negotiables": []},
                project_id="proj-1",
                objective_id="obj-1",
                skill_context=_StubSkillContext(),
                llm_router=object(),
                store=object(),
                workspace_root=self.workspace_root,
            )
        finally:
            mod.RedTeamLoopOrchestrator = original

    def test_first_round_pass_returns_success(self) -> None:
        plans = [{"id": "P_1", "title": "first plan"}]
        round_1 = _round(1, {"plans": plans}, reviewer_verdict="pass")
        loop_result = RedTeamLoopResult(
            success=True,
            rounds_completed=1,
            final_output={"plans": plans},
            history=[round_1],
            stop_reason="predicate_satisfied",
        )
        result = self._run(lambda kwargs: loop_result)
        self.assertTrue(result.success)
        self.assertEqual(plans, result.plans)
        self.assertEqual("pass", result.reviewer_verdict)
        self.assertEqual("predicate_satisfied", result.stop_reason)
        self.assertEqual(1, result.rounds_completed)
        self.assertTrue(result.was_semantically_reviewed)

    def test_reviewer_rejects_then_passes_on_retry(self) -> None:
        plans_v2 = [{"id": "P_2", "title": "second plan"}]
        history = [
            _round(1, {"plans": [{"id": "P_1"}]}, reviewer_verdict="concern"),
            _round(2, {"plans": plans_v2}, reviewer_verdict="pass"),
        ]
        loop_result = RedTeamLoopResult(
            success=True,
            rounds_completed=2,
            final_output={"plans": plans_v2},
            history=history,
            stop_reason="predicate_satisfied",
        )
        result = self._run(lambda kwargs: loop_result)
        self.assertTrue(result.success)
        self.assertEqual(plans_v2, result.plans)
        self.assertEqual("pass", result.reviewer_verdict)
        self.assertEqual(2, result.rounds_completed)

    def test_max_rounds_exhausted_is_failure(self) -> None:
        history = [
            _round(1, {"plans": [{"id": "P_1"}]}, reviewer_verdict="concern"),
            _round(2, {"plans": [{"id": "P_1"}]}, reviewer_verdict="remediation_required"),
        ]
        loop_result = RedTeamLoopResult(
            success=True,
            rounds_completed=2,
            final_output={"plans": [{"id": "P_1"}]},
            history=history,
            stop_reason="max_rounds_exhausted",
        )
        result = self._run(lambda kwargs: loop_result)
        self.assertFalse(result.success)
        self.assertEqual("remediation_required", result.reviewer_verdict)
        self.assertEqual("max_rounds_exhausted", result.stop_reason)
        self.assertTrue(result.was_semantically_reviewed)

    def test_reviewer_failed_last_round_leaves_verdict_empty(self) -> None:
        history = [
            _round(1, {"plans": []}, reviewer_verdict="concern", reviewer_success=False),
        ]
        loop_result = RedTeamLoopResult(
            success=False,
            rounds_completed=1,
            final_output={"plans": []},
            history=history,
            stop_reason="max_rounds_exhausted",
        )
        result = self._run(lambda kwargs: loop_result)
        self.assertFalse(result.success)
        self.assertEqual("", result.reviewer_verdict)
        self.assertFalse(result.was_semantically_reviewed)

    def test_stopping_predicate_requires_pass_verdict(self) -> None:
        """Verify the predicate we pass to the orchestrator only accepts 'pass'."""
        captured: dict[str, Any] = {}

        def factory(kwargs):
            captured.update(kwargs)
            return RedTeamLoopResult(
                success=True,
                rounds_completed=1,
                final_output={"plans": []},
                history=[_round(1, {"plans": []}, reviewer_verdict="pass")],
                stop_reason="predicate_satisfied",
            )

        self._run(factory)
        predicate = captured["stopping_predicate"]

        def _rev(verdict, success=True):
            return {
                "review_plan_atomicity": SkillResult(
                    skill_name="review_plan_atomicity",
                    success=success,
                    output={"verdict": verdict},
                    errors=[] if success else ["x"],
                )
            }

        self.assertTrue(predicate({}, _rev("pass"), 1))
        self.assertTrue(predicate({}, _rev("PASS"), 1))  # case-insensitive
        self.assertFalse(predicate({}, _rev("concern"), 1))
        self.assertFalse(predicate({}, _rev("remediation_required"), 1))
        self.assertFalse(predicate({}, _rev("pass", success=False), 1))
        self.assertFalse(predicate({}, {}, 1))  # missing reviewer

    def test_orchestrator_wired_with_generator_and_reviewer_names(self) -> None:
        captured: dict[str, Any] = {}

        def factory(kwargs):
            captured.update(kwargs)
            return RedTeamLoopResult(
                success=True,
                rounds_completed=1,
                final_output={"plans": []},
                history=[_round(1, {"plans": []}, reviewer_verdict="pass")],
                stop_reason="predicate_satisfied",
            )

        self._run(factory)
        self.assertEqual("plan_draft_trio", captured["generator_skill_name"])
        self.assertEqual(["review_plan_atomicity"], captured["reviewer_skill_names"])
        self.assertEqual("proj-1", captured["project_id"])
        self.assertEqual("obj-1", captured["loop_key"])
        self.assertEqual("trio_plan_orchestrator", captured["loop_label"])


if __name__ == "__main__":
    unittest.main()
