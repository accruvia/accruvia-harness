"""Red-team loop orchestrator.

Generic "generate → review → critique → regenerate" loop on top of the
skill-manager architecture. Replaces the old RedTeamLoopService and the
inline retry loops that were deleted when the direct-LLM paths were
migrated to skills.

A caller provides:

  * a generator skill name (e.g. "interrogation", "mermaid_update_proposal",
    "atomic_decomposition") — the skill that produces the candidate artifact,
  * zero or more reviewer skill names — skills that critique the candidate,
  * a stopping predicate — called with (latest_output, reviewer_outputs,
    round_number) and returns True when the loop should stop,
  * `max_rounds` — absolute ceiling.

Between rounds, `prior_round_findings` and `round_number` are folded into
the generator inputs so the next build_prompt call can surface them. The
three target skills (interrogation, atomic_decomposition,
mermaid_update_proposal) all read these keys from their inputs dict.

Telemetry: opens a parent span "skills_red_team_loop" and per-round spans
"skills_red_team_round". Threads the shared telemetry object into each
invoke_skill call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..domain import Run, RunStatus, Task, TaskStatus, new_id
from ..llm import LLMRouter
from ..skills import SkillInvocation, SkillRegistry, invoke_skill
from ..skills.base import SkillResult


@dataclass(slots=True)
class RedTeamRound:
    round_number: int
    generator_result: SkillResult
    reviewer_results: dict[str, SkillResult] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    stopped: bool = False


@dataclass(slots=True)
class RedTeamLoopResult:
    success: bool
    rounds_completed: int
    final_output: dict[str, Any]
    history: list[RedTeamRound] = field(default_factory=list)
    stop_reason: str = ""


StoppingPredicate = Callable[[dict[str, Any], dict[str, SkillResult], int], bool]
FindingsExtractor = Callable[[dict[str, Any], dict[str, SkillResult]], list[str]]


def default_findings_extractor(
    generator_output: dict[str, Any],
    reviewer_results: dict[str, SkillResult],
) -> list[str]:
    """Collect findings the generator should address in the next round.

    Pulls from two places:
    1. `red_team_findings` on the generator's own output (self-critique).
    2. `findings` on every reviewer's output (external critique).
    """
    findings: list[str] = []
    for item in list(generator_output.get("red_team_findings") or []):
        text = str(item).strip()
        if text:
            findings.append(text)
    for reviewer_name, reviewer_result in reviewer_results.items():
        if not reviewer_result.success:
            findings.append(
                f"reviewer[{reviewer_name}] failed: "
                + "; ".join(reviewer_result.errors)
            )
            continue
        for item in list(reviewer_result.output.get("findings") or []):
            text = str(item).strip()
            if text:
                findings.append(f"[{reviewer_name}] {text}")
    return findings


class RedTeamLoopOrchestrator:
    """Drives a bounded generate → review → regenerate cycle via skills.

    Instances are cheap; one per caller site is fine. Thread safety is the
    caller's responsibility (the harness calls this from a single request
    path at a time).
    """

    def __init__(
        self,
        skill_registry: SkillRegistry,
        llm_router: LLMRouter,
        store: Any,
        workspace_root: Path,
        telemetry: Any = None,
    ) -> None:
        self.skill_registry = skill_registry
        self.llm_router = llm_router
        self.store = store
        self.workspace_root = Path(workspace_root)
        self.telemetry = telemetry

    def execute(
        self,
        *,
        generator_skill_name: str,
        reviewer_skill_names: list[str] | None,
        initial_inputs: dict[str, Any],
        stopping_predicate: StoppingPredicate,
        max_rounds: int,
        project_id: str,
        loop_label: str,
        loop_key: str,
        findings_extractor: FindingsExtractor | None = None,
    ) -> RedTeamLoopResult:
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        generator = self.skill_registry.get(generator_skill_name)
        reviewers = {
            name: self.skill_registry.get(name)
            for name in (reviewer_skill_names or [])
        }
        extract = findings_extractor or default_findings_extractor

        run_dir_root = (
            self.workspace_root / "red_team_loop" / loop_label / loop_key
        )
        run_dir_root.mkdir(parents=True, exist_ok=True)

        history: list[RedTeamRound] = []
        current_inputs: dict[str, Any] = dict(initial_inputs)
        final_output: dict[str, Any] = {}
        stop_reason = "max_rounds_exhausted"

        parent_span = None
        if self.telemetry is not None and hasattr(self.telemetry, "timed"):
            parent_span = self.telemetry.timed(
                "skills_red_team_loop",
                loop_label=loop_label,
                loop_key=loop_key,
                generator=generator_skill_name,
                reviewer_count=len(reviewers),
                max_rounds=max_rounds,
            )
            parent_span.__enter__()

        try:
            for round_number in range(1, max_rounds + 1):
                current_inputs["round_number"] = round_number
                # Prior-round findings already threaded via rebuild below,
                # but ensure the key exists on round 1 so prompts are stable.
                current_inputs.setdefault("prior_round_findings", [])

                round_span = None
                if self.telemetry is not None and hasattr(self.telemetry, "timed"):
                    round_span = self.telemetry.timed(
                        "skills_red_team_round",
                        loop_label=loop_label,
                        loop_key=loop_key,
                        round_number=round_number,
                    )
                    round_span.__enter__()
                try:
                    generator_result = self._invoke(
                        generator,
                        current_inputs,
                        project_id=project_id,
                        loop_label=loop_label,
                        run_dir_root=run_dir_root,
                        round_number=round_number,
                        role="generator",
                    )
                    if not generator_result.success:
                        failure_findings = [
                            "generator_failed: " + "; ".join(generator_result.errors)
                        ]
                        history.append(
                            RedTeamRound(
                                round_number=round_number,
                                generator_result=generator_result,
                                reviewer_results={},
                                findings=failure_findings,
                                stopped=(round_number >= max_rounds),
                            )
                        )
                        if round_number >= max_rounds:
                            stop_reason = "generator_failed"
                            final_output = dict(generator_result.output)
                            break
                        current_inputs = dict(initial_inputs)
                        current_inputs["prior_round_findings"] = failure_findings
                        current_inputs["round_number"] = round_number + 1
                        continue

                    final_output = dict(generator_result.output)

                    reviewer_results: dict[str, SkillResult] = {}
                    for reviewer_name, reviewer_skill in reviewers.items():
                        reviewer_inputs = dict(current_inputs)
                        reviewer_inputs["generator_output"] = generator_result.output
                        reviewer_result = self._invoke(
                            reviewer_skill,
                            reviewer_inputs,
                            project_id=project_id,
                            loop_label=loop_label,
                            run_dir_root=run_dir_root,
                            round_number=round_number,
                            role=f"reviewer_{reviewer_name}",
                        )
                        reviewer_results[reviewer_name] = reviewer_result

                    stop = stopping_predicate(
                        generator_result.output, reviewer_results, round_number
                    )
                    findings = extract(generator_result.output, reviewer_results)
                    history.append(
                        RedTeamRound(
                            round_number=round_number,
                            generator_result=generator_result,
                            reviewer_results=reviewer_results,
                            findings=findings,
                            stopped=stop,
                        )
                    )
                    if stop:
                        stop_reason = "predicate_satisfied"
                        break
                    current_inputs = dict(initial_inputs)
                    current_inputs["prior_round_findings"] = findings
                    current_inputs["round_number"] = round_number + 1
                finally:
                    if round_span is not None:
                        round_span.__exit__(None, None, None)
        finally:
            if parent_span is not None:
                parent_span.__exit__(None, None, None)

        rounds_completed = len(history)
        success = (
            rounds_completed > 0
            and history[-1].generator_result.success
            and stop_reason in {"predicate_satisfied", "max_rounds_exhausted"}
        )
        return RedTeamLoopResult(
            success=success,
            rounds_completed=rounds_completed,
            final_output=final_output,
            history=history,
            stop_reason=stop_reason,
        )

    def _invoke(
        self,
        skill: Any,
        inputs: dict[str, Any],
        *,
        project_id: str,
        loop_label: str,
        run_dir_root: Path,
        round_number: int,
        role: str,
    ) -> SkillResult:
        run_dir = run_dir_root / f"round_{round_number:02d}" / role
        run_dir.mkdir(parents=True, exist_ok=True)
        task = Task(
            id=new_id(f"rtloop_{loop_label}_{role}_task"),
            project_id=project_id,
            title=f"Red-team loop {loop_label} round {round_number} {role}",
            objective=f"Invoke {skill.name} as {role}",
            strategy="red_team_loop",
            status=TaskStatus.COMPLETED,
        )
        run = Run(
            id=new_id(f"rtloop_{loop_label}_{role}_run"),
            task_id=task.id,
            status=RunStatus.COMPLETED,
            attempt=round_number,
            summary=f"Red-team loop {loop_label} round {round_number} {role}",
        )
        invocation = SkillInvocation(
            skill_name=skill.name,
            inputs=inputs,
            task=task,
            run=run,
            run_dir=run_dir,
        )
        return invoke_skill(
            skill, invocation, self.llm_router, telemetry=self.telemetry
        )
