from __future__ import annotations

from dataclasses import dataclass

from .context_control import objective_execution_gate


@dataclass(frozen=True, slots=True)
class FrustrationTriage:
    objective_id: str | None
    likely_causes: list[str]
    recommendation: str
    confidence: float


def triage_frustration(store, *, project_id: str, objective_id: str | None) -> FrustrationTriage:
    likely_causes: list[str] = []
    recommendation = "Open investigation mode and review the current Mermaid against intent and recent evidence."
    confidence = 0.45

    objective = store.get_objective(objective_id) if objective_id else None
    if objective is not None:
        gate = objective_execution_gate(store, objective.id)
        blocked_checks = [
            check["label"]
            for check in gate.gate_checks
            if not str(check["key"]).endswith("_placeholder") and not bool(check["ok"])
        ]
        if blocked_checks:
            likely_causes.append(f"Execution contract is incomplete: {', '.join(blocked_checks)}.")
            confidence = max(confidence, 0.8)

    objective_tasks = [
        task for task in store.list_tasks(project_id) if objective_id is not None and task.objective_id == objective_id
    ]
    if objective_tasks:
        failed_or_blocked = 0
        active_runs = 0
        for task in objective_tasks:
            for run in store.list_runs(task.id):
                if run.status.value in {"failed", "blocked"}:
                    failed_or_blocked += 1
                elif run.status.value in {"planning", "working", "analyzing", "deciding"}:
                    active_runs += 1
        if failed_or_blocked:
            likely_causes.append(
                f"Recent execution evidence is unhealthy: {failed_or_blocked} linked run(s) failed or blocked."
            )
            confidence = max(confidence, 0.7)
        if active_runs and objective is not None:
            likely_causes.append(
                f"There are {active_runs} in-flight run(s) tied to this objective, so observed behavior may still diverge."
            )
            confidence = max(confidence, 0.6)

    if objective is not None and not likely_causes:
        likely_causes.append("The current plan or Mermaid likely no longer matches the operator's intended workflow.")
        confidence = max(confidence, 0.55)

    if not likely_causes:
        likely_causes.append("Operator experience does not match the current implementation or process model.")

    return FrustrationTriage(
        objective_id=objective_id,
        likely_causes=likely_causes,
        recommendation=recommendation,
        confidence=confidence,
    )
