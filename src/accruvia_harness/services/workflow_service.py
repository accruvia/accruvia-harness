from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..context_control import objective_execution_gate, task_bypasses_objective_execution_gate
from ..domain import ObjectiveStatus, Task, TaskStatus


@dataclass(slots=True)
class ObjectiveReadiness:
    stage: str
    ready: bool
    checks: list[dict[str, object]]


class WorkflowService:
    def __init__(self, store) -> None:
        self.store = store

    def planning_readiness(self, objective_id: str) -> ObjectiveReadiness:
        execution = objective_execution_gate(self.store, objective_id)
        checks = [check for check in execution.gate_checks if not str(check.get("key") or "").endswith("_placeholder")]
        return ObjectiveReadiness(stage="planning", ready=execution.ready, checks=checks)

    def execution_readiness(self, objective_id: str, linked_tasks: list[Task] | None = None) -> ObjectiveReadiness:
        base = self.planning_readiness(objective_id)
        linked = list(linked_tasks or self._linked_tasks(objective_id))
        checks = list(base.checks)
        checks.append(
            {
                "key": "linked_tasks_exist",
                "label": "Linked tasks exist",
                "ok": bool(linked),
                "detail": "" if linked else "Execution needs at least one linked task to run.",
            }
        )
        ready = all(bool(check["ok"]) for check in checks)
        return ObjectiveReadiness(stage="execution", ready=ready, checks=checks)

    def review_readiness(self, objective_id: str, linked_tasks: list[Task] | None = None) -> ObjectiveReadiness:
        linked = list(linked_tasks or self._linked_tasks(objective_id))
        unresolved_failed = 0
        completed = 0
        pending = 0
        active = 0
        for task in linked:
            if task.status == TaskStatus.COMPLETED:
                completed += 1
            elif task.status == TaskStatus.PENDING:
                pending += 1
            elif task.status == TaskStatus.ACTIVE:
                active += 1
            elif task.status == TaskStatus.FAILED and not self._is_waived_failed_task(task):
                unresolved_failed += 1
        checks = [
            {
                "key": "linked_tasks_exist",
                "label": "Linked tasks exist",
                "ok": bool(linked),
                "detail": "" if linked else "Review cannot start before the objective has linked tasks.",
            },
            {
                "key": "no_active_tasks",
                "label": "No active tasks",
                "ok": active == 0,
                "detail": "" if active == 0 else f"{active} task(s) are still active.",
            },
            {
                "key": "no_pending_tasks",
                "label": "No pending tasks",
                "ok": pending == 0,
                "detail": "" if pending == 0 else f"{pending} task(s) are still pending.",
            },
            {
                "key": "no_unresolved_failed_tasks",
                "label": "No unresolved failed tasks",
                "ok": unresolved_failed == 0,
                "detail": "" if unresolved_failed == 0 else f"{unresolved_failed} failed task(s) still need disposition.",
            },
            {
                "key": "completed_task_exists",
                "label": "Completed task exists",
                "ok": completed > 0,
                "detail": "" if completed > 0 else "Review requires at least one completed task.",
            },
        ]
        ready = all(bool(check["ok"]) for check in checks)
        return ObjectiveReadiness(stage="review", ready=ready, checks=checks)

    def queue_state_for_task(self, task: Task, *, review_ready: bool | None = None) -> dict[str, object]:
        if task.status == TaskStatus.ACTIVE:
            return {"state": "running", "reason": "Task is active.", "detail": ""}
        if task.status == TaskStatus.COMPLETED:
            return {"state": "done", "reason": "Task completed.", "detail": ""}
        if task.status == TaskStatus.FAILED:
            return {"state": "failed", "reason": "Task failed.", "detail": ""}
        if not task.objective_id:
            return {"state": "runnable", "reason": "Task is pending and not objective-gated.", "detail": ""}
        if task_bypasses_objective_execution_gate(task):
            return {
                "state": "runnable",
                "reason": "Objective review remediation may run while the parent objective is otherwise gated.",
                "detail": "",
            }
        gate = objective_execution_gate(self.store, task.objective_id)
        if not gate.ready:
            blocking = next((check for check in gate.gate_checks if not check["ok"] and not str(check.get("key") or "").endswith("_placeholder")), None)
            return {
                "state": "blocked_by_gate",
                "reason": str(blocking.get("label") or "Objective execution gate blocked.") if blocking else "Objective execution gate blocked.",
                "detail": str(blocking.get("detail") or "") if blocking else "",
            }
        if review_ready is True:
            return {
                "state": "waiting_on_review",
                "reason": "Objective is ready for promotion review before more execution.",
                "detail": "Automatic promotion review should start now instead of keeping new work pending.",
            }
        return {"state": "runnable", "reason": "Task is eligible to run.", "detail": ""}

    def reconcile_objective(
        self,
        objective_id: str,
        *,
        start_atomic: Callable[[str], None] | None = None,
        start_review: Callable[[str], None] | None = None,
        atomic_running: bool = False,
        review_running: bool = False,
        review_start_allowed: bool = False,
    ) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return {"objective_id": objective_id, "changed": False, "actions": []}
        actions: list[str] = []
        before = objective.status
        linked_tasks = self._linked_tasks(objective_id)
        derived_phase = self.store.update_objective_phase(objective_id) if linked_tasks else objective.status
        if derived_phase is not None and derived_phase != before:
            actions.append(f"phase:{before.value}->{derived_phase.value}")
            objective = self.store.get_objective(objective_id) or objective
        planning = self.planning_readiness(objective_id)
        review = self.review_readiness(objective_id, linked_tasks)
        has_runnable_linked_work = any(task.status in {TaskStatus.PENDING, TaskStatus.ACTIVE} for task in linked_tasks)
        only_terminal_linked_work = bool(linked_tasks) and not has_runnable_linked_work
        should_restart_atomic = (
            planning.ready
            and only_terminal_linked_work
            and objective.status in {ObjectiveStatus.PAUSED, ObjectiveStatus.PLANNING}
            and not review.ready
        )
        # Atomic generation should restart when an objective is left with only
        # terminal failed/completed work. Without this, a paused objective with
        # failed remediation tasks deadlocks forever because historical linked
        # tasks suppress a fresh decomposition pass.
        if planning.ready and start_atomic is not None and not atomic_running and (not linked_tasks or should_restart_atomic):
            start_atomic(objective_id)
            actions.append("restart_atomic_generation" if should_restart_atomic else "start_atomic_generation")
        objective = self.store.get_objective(objective_id) or objective
        if review.ready and objective.status == ObjectiveStatus.RESOLVED and start_review is not None and not review_running and review_start_allowed:
            start_review(objective_id)
            actions.append("start_objective_review")
        return {
            "objective_id": objective_id,
            "changed": bool(actions),
            "actions": actions,
            "objective_status": (self.store.get_objective(objective_id) or objective).status.value,
            "planning_ready": planning.ready,
            "review_ready": review.ready,
        }

    def _linked_tasks(self, objective_id: str) -> list[Task]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return []
        return [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective_id]

    def _is_waived_failed_task(self, task: Task) -> bool:
        metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
        workflow_disposition = metadata.get("workflow_state_disposition") if isinstance(metadata, dict) else None
        if isinstance(workflow_disposition, dict) and str(workflow_disposition.get("kind") or "").strip() == "ignore_obsolete":
            return True
        disposition = metadata.get("failed_task_disposition") if isinstance(metadata, dict) else None
        return bool(
            task.status == TaskStatus.FAILED
            and isinstance(disposition, dict)
            and str(disposition.get("kind") or "").strip() == "waive_obsolete"
        )
