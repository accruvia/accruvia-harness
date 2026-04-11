from __future__ import annotations

from .domain import ContextRecord, new_id


class ContextRecorder:
    def __init__(self, store) -> None:
        self.store = store

    def record_operator_comment(
        self,
        *,
        project_id: str,
        objective_id: str | None = None,
        task_id: str | None = None,
        author: str | None = None,
        content: str,
    ) -> ContextRecord:
        record = ContextRecord(
            id=new_id("context"),
            record_type="operator_comment",
            project_id=project_id,
            objective_id=objective_id,
            task_id=task_id,
            visibility="model_visible",
            author_type="operator",
            author_id=(author or "").strip(),
            content=content,
        )
        self.store.create_context_record(record)
        return record
