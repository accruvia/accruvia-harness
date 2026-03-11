from __future__ import annotations

from ..domain import Task


def task_created_payload(task: Task) -> dict[str, object]:
    return {
        "project_id": task.project_id,
        "priority": task.priority,
        "parent_task_id": task.parent_task_id,
        "source_run_id": task.source_run_id,
        "external_ref_type": task.external_ref_type,
        "external_ref_id": task.external_ref_id,
        "external_ref_metadata": task.external_ref_metadata,
        "validation_profile": task.validation_profile,
        "strategy": task.strategy,
        "max_attempts": task.max_attempts,
        "max_branches": task.max_branches,
        "required_artifacts": task.required_artifacts,
    }
