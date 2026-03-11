from __future__ import annotations

from dataclasses import dataclass

from .domain import Artifact, DecisionAction, Run, Task


@dataclass(slots=True)
class PlanResult:
    summary: str
    retry_focus: str | None = None
    retry_context: dict[str, object] | None = None


@dataclass(slots=True)
class WorkResult:
    summary: str
    artifacts: list[tuple[str, str, str]]
    outcome: str = "success"
    diagnostics: dict[str, object] | None = None


@dataclass(slots=True)
class AnalyzeResult:
    verdict: str
    confidence: float
    summary: str
    details: dict[str, object]


@dataclass(slots=True)
class DecideResult:
    action: DecisionAction
    rationale: str


@dataclass(slots=True)
class RetryContext:
    attempt: int
    previous_run_id: str | None
    previous_verdict: str | None
    previous_decision: str | None
    focus: str | None
    details: dict[str, object]


class DefaultPlanner:
    def plan(self, task: Task, retry_context: RetryContext | None = None) -> PlanResult:
        if retry_context is None or retry_context.attempt <= 1:
            return PlanResult(
                summary=(
                    f"Plan task '{task.title}' using strategy '{task.strategy}' "
                    f"against objective: {task.objective}"
                )
            )
        focus = retry_context.focus or "address the last failed evaluation outcome"
        return PlanResult(
            summary=(
                f"Retry attempt {retry_context.attempt} for task '{task.title}' using strategy "
                f"'{task.strategy}'. Focus on {focus}. Previous verdict was "
                f"'{retry_context.previous_verdict or 'unknown'}' with decision "
                f"'{retry_context.previous_decision or 'unknown'}'. Objective: {task.objective}"
            ),
            retry_focus=focus,
            retry_context={
                "attempt": retry_context.attempt,
                "previous_run_id": retry_context.previous_run_id,
                "previous_verdict": retry_context.previous_verdict,
                "previous_decision": retry_context.previous_decision,
                "focus": focus,
                **retry_context.details,
            },
        )

class DefaultAnalyzer:
    def analyze(self, task: Task, run: Run, artifacts: list[Artifact]) -> AnalyzeResult:
        artifact_kinds = sorted({artifact.kind for artifact in artifacts})
        missing = sorted(set(task.required_artifacts) - set(artifact_kinds))
        artifact_count = len(artifacts)
        if artifact_count == 0:
            return AnalyzeResult(
                verdict="failed",
                confidence=0.95,
                summary="Run produced no artifacts.",
                details={"artifact_count": artifact_count},
            )
        if missing:
            return AnalyzeResult(
                verdict="incomplete",
                confidence=0.9,
                summary="Run is missing required artifacts.",
                details={
                    "artifact_count": artifact_count,
                    "artifact_kinds": artifact_kinds,
                    "missing_required_artifacts": missing,
                },
            )
        return AnalyzeResult(
            verdict="acceptable",
            confidence=0.8,
            summary="Run produced the required durable artifacts.",
            details={
                "artifact_count": artifact_count,
                "artifact_kinds": artifact_kinds,
                "task_title": task.title,
                "strategy": task.strategy,
            },
        )


class DefaultDecider:
    def decide(self, analysis: AnalyzeResult, run: Run, task: Task) -> DecideResult:
        if analysis.verdict == "acceptable":
            return DecideResult(
                action=DecisionAction.PROMOTE,
                rationale="Required artifacts exist and analysis passed.",
            )
        if run.attempt >= task.max_attempts:
            return DecideResult(
                action=DecisionAction.FAIL,
                rationale="Retry budget exhausted.",
            )
        return DecideResult(
            action=DecisionAction.RETRY,
            rationale="Artifacts were insufficient; retry within bounded task budget.",
        )


class RetryStrategyAdvisor:
    def advise(
        self,
        task: Task,
        attempt: int,
        previous_run: Run | None,
        previous_evaluation: AnalyzeResult | None,
        previous_decision: DecisionAction | None,
    ) -> RetryContext | None:
        if attempt <= 1 or previous_run is None:
            return None
        focus = "produce the required durable artifacts"
        details: dict[str, object] = {}
        verdict = previous_evaluation.verdict if previous_evaluation is not None else None
        if previous_evaluation is not None:
            missing = previous_evaluation.details.get("missing_required_artifacts")
            if isinstance(missing, list) and missing:
                missing_list = [str(item) for item in missing]
                focus = f"producing the missing required artifacts: {', '.join(missing_list)}"
                details["missing_required_artifacts"] = missing_list
            elif verdict == "failed":
                focus = "producing at least one valid report and plan artifact before deeper changes"
            else:
                artifact_kinds = previous_evaluation.details.get("artifact_kinds")
                if isinstance(artifact_kinds, list) and artifact_kinds:
                    focus = f"improving the previous artifact set: {', '.join(str(item) for item in artifact_kinds)}"
                    details["artifact_kinds"] = [str(item) for item in artifact_kinds]
        return RetryContext(
            attempt=attempt,
            previous_run_id=previous_run.id,
            previous_verdict=verdict,
            previous_decision=previous_decision.value if previous_decision else None,
            focus=focus,
            details=details,
        )
