from __future__ import annotations

from ..domain import ContextRecord, Event, ObjectiveStatus, TaskStatus, new_id


class ObjectivePromotionService:
    def __init__(self, store) -> None:
        self.store = store

    def _return_objective_to_execution_loop(
        self,
        objective_id: str,
        *,
        source_task_id: str | None = None,
        source_run_id: str | None = None,
    ) -> ObjectiveStatus:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        tasks = self.store.list_tasks(project_id=objective.project_id)
        linked = [task for task in tasks if task.objective_id == objective_id]
        next_status = ObjectiveStatus.PLANNING
        if any(task.status == TaskStatus.ACTIVE for task in linked):
            next_status = ObjectiveStatus.EXECUTING
        self.store.update_objective_status(objective_id, next_status)
        payload = {
            "objective_id": objective_id,
            "status": next_status.value,
            "source_task_id": source_task_id,
            "source_run_id": source_run_id,
        }
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="objective",
                entity_id=objective_id,
                event_type="objective_reentered_execution_loop",
                payload=payload,
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_execution_reentered",
                project_id=objective.project_id,
                objective_id=objective_id,
                visibility="operator_visible",
                author_type="system",
                content=f"Objective returned to the execution loop as {next_status.value}.",
                metadata=payload,
            )
        )
        return next_status
