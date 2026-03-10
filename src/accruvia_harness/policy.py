from __future__ import annotations

from dataclasses import dataclass

from .domain import Artifact, DecisionAction, Run, Task


@dataclass(slots=True)
class PlanResult:
    summary: str


@dataclass(slots=True)
class WorkResult:
    summary: str
    artifacts: list[tuple[str, str, str]]


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


class DefaultPlanner:
    def plan(self, task: Task) -> PlanResult:
        return PlanResult(
            summary=(
                f"Plan task '{task.title}' using strategy '{task.strategy}' "
                f"against objective: {task.objective}"
            )
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
