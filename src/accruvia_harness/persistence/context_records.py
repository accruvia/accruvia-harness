from __future__ import annotations

import json
from datetime import UTC, datetime

from .common import context_record_from_row, intent_model_from_row, mermaid_artifact_from_row, objective_from_row
from ..domain import ContextRecord, IntentModel, MermaidArtifact, MermaidStatus, Objective, ObjectiveStatus


class ContextRecordsStoreMixin:
    def create_objective(self, objective: Objective) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO objectives (
                    id, project_id, title, summary, priority, status, phase, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    objective.id,
                    objective.project_id,
                    objective.title,
                    objective.summary,
                    objective.priority,
                    objective.status.value,
                    objective.phase.value,
                    objective.created_at.isoformat(),
                    objective.updated_at.isoformat(),
                ),
            )

    def list_objectives(self, project_id: str | None = None) -> list[Objective]:
        query = """
            SELECT id, project_id, title, summary, priority, status, phase, created_at, updated_at
            FROM objectives
        """
        params: tuple[str, ...] = ()
        if project_id:
            query += " WHERE project_id = ?"
            params = (project_id,)
        query += " ORDER BY priority DESC, created_at"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [objective_from_row(row) for row in rows]

    def get_objective(self, objective_id: str) -> Objective | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, project_id, title, summary, priority, status, created_at, updated_at
                FROM objectives
                WHERE id = ?
                """,
                (objective_id,),
            ).fetchone()
        return objective_from_row(row) if row else None

    def update_objective_status(self, objective_id: str, status: ObjectiveStatus) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE objectives SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, datetime.now(UTC).isoformat(), objective_id),
            )

    def set_objective_phase(self, objective_id: str, phase: "ObjectivePhase", status: ObjectiveStatus | None = None) -> None:
        from ..domain import ObjectivePhase as _OP, PHASE_TO_STATUS
        resolved_status = status or PHASE_TO_STATUS.get(_OP(phase.value if isinstance(phase, _OP) else phase))
        with self.connect() as connection:
            if resolved_status is not None:
                connection.execute(
                    "UPDATE objectives SET phase = ?, status = ?, updated_at = ? WHERE id = ?",
                    (phase.value, resolved_status.value, datetime.now(UTC).isoformat(), objective_id),
                )
            else:
                connection.execute(
                    "UPDATE objectives SET phase = ?, updated_at = ? WHERE id = ?",
                    (phase.value, datetime.now(UTC).isoformat(), objective_id),
                )

    def create_intent_model(self, intent_model: IntentModel) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO intent_models (
                    id, objective_id, version, intent_summary, success_definition,
                    non_negotiables_json, preferred_tradeoffs_json, unacceptable_outcomes_json,
                    known_unknowns_json, operator_examples_json, frustration_signals_json,
                    sop_constraints_json, current_confidence, author_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_model.id,
                    intent_model.objective_id,
                    intent_model.version,
                    intent_model.intent_summary,
                    intent_model.success_definition,
                    json.dumps(intent_model.non_negotiables, sort_keys=True),
                    json.dumps(intent_model.preferred_tradeoffs, sort_keys=True),
                    json.dumps(intent_model.unacceptable_outcomes, sort_keys=True),
                    json.dumps(intent_model.known_unknowns, sort_keys=True),
                    json.dumps(intent_model.operator_examples, sort_keys=True),
                    json.dumps(intent_model.frustration_signals, sort_keys=True),
                    json.dumps(intent_model.sop_constraints, sort_keys=True),
                    intent_model.current_confidence,
                    intent_model.author_type,
                    intent_model.created_at.isoformat(),
                ),
            )

    def list_intent_models(self, objective_id: str) -> list[IntentModel]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, objective_id, version, intent_summary, success_definition,
                       non_negotiables_json, preferred_tradeoffs_json, unacceptable_outcomes_json,
                       known_unknowns_json, operator_examples_json, frustration_signals_json,
                       sop_constraints_json, current_confidence, author_type, created_at
                FROM intent_models
                WHERE objective_id = ?
                ORDER BY version, created_at
                """,
                (objective_id,),
            ).fetchall()
        return [intent_model_from_row(row) for row in rows]

    def latest_intent_model(self, objective_id: str) -> IntentModel | None:
        models = self.list_intent_models(objective_id)
        return models[-1] if models else None

    def next_intent_model_version(self, objective_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM intent_models WHERE objective_id = ?",
                (objective_id,),
            ).fetchone()
        return int(row["version"]) + 1

    def create_mermaid_artifact(self, artifact: MermaidArtifact) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO mermaid_artifacts (
                    id, objective_id, diagram_type, version, status, summary, content,
                    required_for_execution, blocking_reason, author_type, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    artifact.objective_id,
                    artifact.diagram_type,
                    artifact.version,
                    artifact.status.value,
                    artifact.summary,
                    artifact.content,
                    1 if artifact.required_for_execution else 0,
                    artifact.blocking_reason,
                    artifact.author_type,
                    artifact.created_at.isoformat(),
                    artifact.updated_at.isoformat(),
                ),
            )

    def list_mermaid_artifacts(self, objective_id: str, diagram_type: str | None = None) -> list[MermaidArtifact]:
        query = """
            SELECT id, objective_id, diagram_type, version, status, summary, content,
                   required_for_execution, blocking_reason, author_type, created_at, updated_at
            FROM mermaid_artifacts
            WHERE objective_id = ?
        """
        params: list[str] = [objective_id]
        if diagram_type:
            query += " AND diagram_type = ?"
            params.append(diagram_type)
        query += " ORDER BY version, created_at"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [mermaid_artifact_from_row(row) for row in rows]

    def latest_mermaid_artifact(self, objective_id: str, diagram_type: str | None = None) -> MermaidArtifact | None:
        artifacts = self.list_mermaid_artifacts(objective_id, diagram_type)
        return artifacts[-1] if artifacts else None

    def get_mermaid_artifact(self, artifact_id: str) -> MermaidArtifact | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, objective_id, diagram_type, version, status, summary, content,
                       required_for_execution, blocking_reason, author_type, created_at, updated_at
                FROM mermaid_artifacts
                WHERE id = ?
                """,
                (artifact_id,),
            ).fetchone()
        return mermaid_artifact_from_row(row) if row else None

    def next_mermaid_version(self, objective_id: str, diagram_type: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS version
                FROM mermaid_artifacts
                WHERE objective_id = ? AND diagram_type = ?
                """,
                (objective_id, diagram_type),
            ).fetchone()
        return int(row["version"]) + 1

    def mark_mermaid_artifact_status(self, artifact_id: str, status: MermaidStatus) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE mermaid_artifacts SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, datetime.now(UTC).isoformat(), artifact_id),
            )

    def create_context_record(self, record: ContextRecord) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO context_records (
                    id, record_type, project_id, objective_id, task_id, run_id,
                    visibility, author_type, author_id, content, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.record_type,
                    record.project_id,
                    record.objective_id,
                    record.task_id,
                    record.run_id,
                    record.visibility,
                    record.author_type,
                    record.author_id,
                    record.content,
                    json.dumps(record.metadata, sort_keys=True),
                    record.created_at.isoformat(),
                ),
            )

    def list_context_records(
        self,
        *,
        project_id: str | None = None,
        objective_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        record_type: str | None = None,
    ) -> list[ContextRecord]:
        query = """
            SELECT id, record_type, project_id, objective_id, task_id, run_id,
                   visibility, author_type, author_id, content, metadata_json, created_at
            FROM context_records
        """
        clauses: list[str] = []
        params: list[str] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if objective_id:
            clauses.append("objective_id = ?")
            params.append(objective_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if record_type:
            clauses.append("record_type = ?")
            params.append(record_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [context_record_from_row(row) for row in rows]
