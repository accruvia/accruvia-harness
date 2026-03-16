from __future__ import annotations

from dataclasses import dataclass

from .domain import MermaidStatus


@dataclass(frozen=True, slots=True)
class ObjectiveExecutionGate:
    objective_id: str
    ready: bool
    gate_checks: list[dict[str, object]]


def objective_execution_gate(store, objective_id: str) -> ObjectiveExecutionGate:
    checks: list[dict[str, object]] = []
    objective = store.get_objective(objective_id)
    checks.append(
        {
            "key": "objective_exists",
            "label": "Objective exists",
            "ok": objective is not None,
            "detail": "" if objective is not None else "Objective record is missing.",
        }
    )
    intent_model = store.latest_intent_model(objective_id)
    checks.append(
        {
            "key": "intent_model",
            "label": "Intent model",
            "ok": intent_model is not None,
            "detail": "" if intent_model is not None else "Intent model is required before execution.",
        }
    )
    interrogation_complete = bool(
        store.list_context_records(objective_id=objective_id, record_type="interrogation_completed")
    )
    checks.append(
        {
            "key": "interrogation_complete",
            "label": "Interrogation complete",
            "ok": interrogation_complete,
            "detail": (
                ""
                if interrogation_complete
                else "The harness must interrogate and red-team the objective before Mermaid review."
            ),
        }
    )
    mermaid = store.latest_mermaid_artifact(objective_id)
    checks.append(
        {
            "key": "required_mermaid",
            "label": "Required Mermaid",
            "ok": mermaid is not None and mermaid.required_for_execution,
            "detail": (
                ""
                if mermaid is not None and mermaid.required_for_execution
                else "A required Mermaid artifact must exist before execution."
            ),
        }
    )
    checks.append(
        {
            "key": "mermaid_finished",
            "label": "Mermaid finished",
            "ok": mermaid is not None and mermaid.status == MermaidStatus.FINISHED,
            "detail": (
                ""
                if mermaid is not None and mermaid.status == MermaidStatus.FINISHED
                else "Execution is blocked until the current Mermaid is finished."
            ),
        }
    )
    checks.append(
        {
            "key": "plan_placeholder",
            "label": "Plan",
            "ok": False,
            "detail": "Plan gate not implemented yet.",
        }
    )
    checks.append(
        {
            "key": "atomic_slice_placeholder",
            "label": "Atomic slice",
            "ok": False,
            "detail": "Atomic slice gate not implemented yet.",
        }
    )
    implemented_checks = [item for item in checks if not item["key"].endswith("_placeholder")]
    ready = all(bool(item["ok"]) for item in implemented_checks)
    return ObjectiveExecutionGate(objective_id=objective_id, ready=ready, gate_checks=checks)
