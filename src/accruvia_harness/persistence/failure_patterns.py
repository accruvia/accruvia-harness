from __future__ import annotations

import json

from .common import failure_pattern_from_row
from ..domain import FailurePatternRecord


class FailurePatternsStoreMixin:
    def create_failure_pattern(self, record: FailurePatternRecord) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO failure_patterns (
                    id, task_id, run_id, objective_id, attempt, category, fingerprint,
                    summary, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.task_id,
                    record.run_id,
                    record.objective_id,
                    record.attempt,
                    record.category.value,
                    record.fingerprint,
                    record.summary,
                    json.dumps(record.details, sort_keys=True),
                    record.created_at.isoformat(),
                ),
            )

    def list_failure_patterns(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        objective_id: str | None = None,
    ) -> list[FailurePatternRecord]:
        query = """
            SELECT id, task_id, run_id, objective_id, attempt, category, fingerprint,
                   summary, details_json, created_at
            FROM failure_patterns
        """
        clauses: list[str] = []
        params: list[str] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if objective_id:
            clauses.append("objective_id = ?")
            params.append(objective_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [failure_pattern_from_row(row) for row in rows]
