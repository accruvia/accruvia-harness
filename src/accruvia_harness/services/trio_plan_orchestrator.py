"""Canonical TRIO planning orchestrator: plan_draft_trio + review_plan_atomicity.

This is the single entry point for callers who want a TRIO-shaped plan
decomposition with both schema enforcement AND semantic review. It wraps
the generator skill, the reviewer skill, and the RedTeamLoopOrchestrator
with the canonical stopping predicate. Every caller that would otherwise
roll its own `invoke_skill(plan_draft_trio, ...)` should use this helper
instead so semantic review is never skipped.

Stopping rule:
  The loop stops when the generator produces output that passes its own
  validator AND the reviewer returns verdict="pass" (no major or critical
  findings). Any other reviewer verdict triggers a retry with the
  reviewer's findings threaded back into the generator's prior_round_
  findings input.

Why this helper exists (the gap it closes):
  The ReviewPlanAtomicitySkill was built in commit 6c21a2a but never
  wired into the default TRIO invocation path. Individual callers were
  either skipping semantic review entirely (invoke_skill direct) or
  re-implementing the orchestrator wiring locally (the A/B harness).
  Both leaked the reviewer's value. This module makes it the path of
  least resistance.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..llm import LLMRouter
from ..skills import (
    PlanDraftTrioSkill,
    ReviewPlanAtomicitySkill,
    SkillContext,
)
from ..skills.registry import SkillRegistry
from .red_team_loop import RedTeamLoopOrchestrator, RedTeamLoopResult


_MAX_ROUNDS = 4
_GENERATOR_NAME = "plan_draft_trio"
_REVIEWER_NAME = "review_plan_atomicity"


@dataclass(slots=True)
class TrioPlanningResult:
    """Outcome of a canonical TRIO planning call.

    Wraps the RedTeamLoopOrchestrator's result with the bits callers
    actually want: the final plan list, whether it passed review, and
    enough history to debug why it took N rounds if it did.
    """

    success: bool
    plans: list[dict[str, Any]]
    rounds_completed: int
    reviewer_verdict: str
    stop_reason: str
    loop_result: RedTeamLoopResult

    @property
    def was_semantically_reviewed(self) -> bool:
        """True if at least one round produced a reviewer result.

        Distinguishes "generator succeeded on round 1 and loop stopped
        before the reviewer even saw the output" from "reviewer looked
        at the output and said pass."
        """
        return self.reviewer_verdict != ""


def generate_trio_plans(
    *,
    intent_inputs: dict[str, Any],
    project_id: str,
    objective_id: str,
    skill_context: SkillContext,
    llm_router: LLMRouter,
    store: Any,
    workspace_root: Path,
    telemetry: Any = None,
    max_rounds: int = _MAX_ROUNDS,
) -> TrioPlanningResult:
    """Run plan_draft_trio through the canonical review-then-retry loop.

    Builds a scratch SkillRegistry containing just the generator +
    reviewer (so the caller doesn't need to pre-register them in a
    shared registry) and drives RedTeamLoopOrchestrator with the
    canonical stopping predicate.

    Args:
      intent_inputs: the dict plan_draft_trio's build_prompt expects —
        objective_title, objective_summary, intent_summary,
        success_definition, non_negotiables, frustration_signals,
        optional interrogation_questions / interrogation_red_team_findings.
        The reviewer reads the same dict plus the generator's output.
      project_id, objective_id: used for telemetry labels + run_dir layout.
      skill_context: required; plan_draft_trio will raise at construction
        without it.
      llm_router, store, workspace_root, telemetry: standard harness
        wiring, typically pulled from HarnessEngine.
      max_rounds: hard cap on retry rounds. Default 4 matches the
        existing atomic decomposition constants.

    Returns:
      TrioPlanningResult. `success=True` means the generator converged
      and the reviewer approved. `success=False` means either the
      generator failed max_rounds times or the reviewer rejected every
      round.
    """
    generator = PlanDraftTrioSkill(context=skill_context)
    reviewer = ReviewPlanAtomicitySkill()
    registry = SkillRegistry()
    registry.register(generator)
    registry.register(reviewer)

    orchestrator = RedTeamLoopOrchestrator(
        skill_registry=registry,
        llm_router=llm_router,
        store=store,
        workspace_root=workspace_root,
        telemetry=telemetry,
    )

    def stopping_predicate(
        output: dict[str, Any],
        reviewer_results: dict[str, Any],
        round_number: int,
    ) -> bool:
        """Stop when the reviewer approves.

        The generator's own success is a prerequisite (handled by the
        orchestrator's retry-on-generator-failure path). Beyond that,
        we require the reviewer to explicitly say verdict=="pass".
        Concern or remediation_required verdicts keep the loop going.
        """
        rev = reviewer_results.get(_REVIEWER_NAME)
        if rev is None or not rev.success:
            return False
        verdict = str(rev.output.get("verdict") or "").strip().lower()
        return verdict == "pass"

    loop_result = orchestrator.execute(
        generator_skill_name=_GENERATOR_NAME,
        reviewer_skill_names=[_REVIEWER_NAME],
        initial_inputs=dict(intent_inputs),
        stopping_predicate=stopping_predicate,
        max_rounds=max_rounds,
        project_id=project_id,
        loop_label="trio_plan_orchestrator",
        loop_key=objective_id,
    )

    plans = list((loop_result.final_output or {}).get("plans") or [])
    reviewer_verdict = ""
    if loop_result.history:
        last_round = loop_result.history[-1]
        rev_result = last_round.reviewer_results.get(_REVIEWER_NAME)
        if rev_result is not None and rev_result.success:
            reviewer_verdict = str(rev_result.output.get("verdict") or "").strip().lower()

    # Success = loop stopped via predicate (reviewer passed), not max rounds
    # exhaustion or generator failure. max_rounds_exhausted with the final
    # round's generator succeeding is still "we gave up," not success.
    converged = loop_result.stop_reason == "predicate_satisfied"

    return TrioPlanningResult(
        success=converged,
        plans=plans,
        rounds_completed=loop_result.rounds_completed,
        reviewer_verdict=reviewer_verdict,
        stop_reason=loop_result.stop_reason,
        loop_result=loop_result,
    )
