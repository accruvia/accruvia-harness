from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .domain import (
    Artifact,
    Decision,
    DecisionAction,
    Event,
    Evaluation,
    PromotionRecord,
    PromotionStatus,
    Project,
    Run,
    RunStatus,
    Task,
    TaskLease,
    TaskStatus,
)
from .migrations import MIGRATIONS, apply_migrations


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class SQLiteHarnessStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            apply_migrations(connection)

    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
        return int(row["version"])

    def expected_schema_version(self) -> int:
        return max(migration.version for migration in MIGRATIONS)

    def create_project(self, project: Project) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO projects (id, name, description, created_at) VALUES (?, ?, ?, ?)",
                (project.id, project.name, project.description, project.created_at.isoformat()),
            )

    def create_task(self, task: Task) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, objective, priority, parent_task_id, source_run_id,
                    external_ref_type, external_ref_id,
                    strategy, max_attempts, required_artifacts_json, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.project_id,
                    task.title,
                    task.objective,
                    task.priority,
                    task.parent_task_id,
                    task.source_run_id,
                    task.external_ref_type,
                    task.external_ref_id,
                    task.strategy,
                    task.max_attempts,
                    json.dumps(task.required_artifacts, sort_keys=True),
                    task.status.value,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )

    def list_projects(self) -> list[Project]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, description, created_at FROM projects ORDER BY created_at"
            ).fetchall()
        return [
            Project(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def list_tasks(self, project_id: str | None = None) -> list[Task]:
        query = """
            SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                   external_ref_type, external_ref_id,
                   strategy, max_attempts,
                   required_artifacts_json, status, created_at, updated_at
            FROM tasks
        """
        params: tuple[str, ...] = ()
        if project_id:
            query += " WHERE project_id = ?"
            params = (project_id,)
        query += " ORDER BY priority DESC, created_at"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._task_from_row(row) for row in rows]

    def get_task(self, task_id: str) -> Task | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                       external_ref_type, external_ref_id,
                       strategy, max_attempts,
                       required_artifacts_json, status, created_at, updated_at
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def get_task_by_external_ref(self, ref_type: str, ref_id: str) -> Task | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                       external_ref_type, external_ref_id,
                       strategy, max_attempts, required_artifacts_json, status, created_at, updated_at
                FROM tasks
                WHERE external_ref_type = ? AND external_ref_id = ?
                ORDER BY created_at
                LIMIT 1
                """,
                (ref_type, ref_id),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def next_pending_task(self, project_id: str | None = None) -> Task | None:
        query = """
            SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                   external_ref_type, external_ref_id,
                   strategy, max_attempts, required_artifacts_json, status, created_at, updated_at
            FROM tasks
            WHERE status = ?
        """
        params: list[str] = [TaskStatus.PENDING.value]
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        query += " ORDER BY priority DESC, created_at LIMIT 1"
        with self.connect() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
        return self._task_from_row(row) if row else None

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, datetime.now(UTC).isoformat(), task_id),
            )

    def acquire_task_lease(
        self,
        worker_id: str,
        lease_seconds: int,
        project_id: str | None = None,
    ) -> Task | None:
        now = datetime.now(UTC)
        expires_at = datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=UTC)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM task_leases WHERE lease_expires_at <= ?",
                (now.isoformat(),),
            )
            query = """
                SELECT t.id
                FROM tasks t
                LEFT JOIN task_leases l ON l.task_id = t.id
                WHERE t.status = ? AND l.task_id IS NULL
            """
            params: list[str] = [TaskStatus.PENDING.value]
            if project_id:
                query += " AND t.project_id = ?"
                params.append(project_id)
            query += " ORDER BY t.priority DESC, t.created_at LIMIT 1"
            row = connection.execute(query, tuple(params)).fetchone()
            if row is None:
                return None
            task_id = row["id"]
            connection.execute(
                """
                INSERT OR REPLACE INTO task_leases (task_id, worker_id, lease_expires_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, worker_id, expires_at.isoformat(), now.isoformat()),
            )
        return self.get_task(task_id)

    def release_task_lease(self, task_id: str, worker_id: str | None = None) -> None:
        with self.connect() as connection:
            if worker_id is None:
                connection.execute("DELETE FROM task_leases WHERE task_id = ?", (task_id,))
                return
            connection.execute(
                "DELETE FROM task_leases WHERE task_id = ? AND worker_id = ?",
                (task_id, worker_id),
            )

    def list_task_leases(self) -> list[TaskLease]:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("DELETE FROM task_leases WHERE lease_expires_at <= ?", (now,))
            rows = connection.execute(
                """
                SELECT task_id, worker_id, lease_expires_at, created_at
                FROM task_leases
                ORDER BY created_at
                """
            ).fetchall()
        return [
            TaskLease(
                task_id=row["task_id"],
                worker_id=row["worker_id"],
                lease_expires_at=_parse_dt(row["lease_expires_at"]),
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def next_attempt(self, task_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS attempt FROM runs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["attempt"]) + 1

    def create_run(self, run: Run) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (id, task_id, status, attempt, summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.task_id,
                    run.status.value,
                    run.attempt,
                    run.summary,
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                ),
            )

    def update_run(self, run: Run) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?, summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    run.status.value,
                    run.summary,
                    run.updated_at.isoformat(),
                    run.id,
                ),
            )

    def list_runs(self, task_id: str | None = None) -> list[Run]:
        query = """
            SELECT id, task_id, status, attempt, summary, created_at, updated_at
            FROM runs
        """
        params: tuple[str, ...] = ()
        if task_id:
            query += " WHERE task_id = ?"
            params = (task_id,)
        query += " ORDER BY created_at"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._run_from_row(row) for row in rows]

    def get_run(self, run_id: str) -> Run | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, task_id, status, attempt, summary, created_at, updated_at
                FROM runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        return self._run_from_row(row) if row else None

    def create_artifact(self, artifact: Artifact) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (id, run_id, kind, path, summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    artifact.run_id,
                    artifact.kind,
                    artifact.path,
                    artifact.summary,
                    artifact.created_at.isoformat(),
                ),
            )

    def list_artifacts(self, run_id: str) -> list[Artifact]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, kind, path, summary, created_at
                FROM artifacts WHERE run_id = ? ORDER BY created_at
                """,
                (run_id,),
            ).fetchall()
        return [
            Artifact(
                id=row["id"],
                run_id=row["run_id"],
                kind=row["kind"],
                path=row["path"],
                summary=row["summary"],
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def create_evaluation(self, evaluation: Evaluation) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO evaluations (id, run_id, verdict, confidence, summary, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.id,
                    evaluation.run_id,
                    evaluation.verdict,
                    evaluation.confidence,
                    evaluation.summary,
                    json.dumps(evaluation.details, sort_keys=True),
                    evaluation.created_at.isoformat(),
                ),
            )

    def list_evaluations(self, run_id: str) -> list[Evaluation]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, verdict, confidence, summary, details_json, created_at
                FROM evaluations WHERE run_id = ? ORDER BY created_at
                """,
                (run_id,),
            ).fetchall()
        return [
            Evaluation(
                id=row["id"],
                run_id=row["run_id"],
                verdict=row["verdict"],
                confidence=float(row["confidence"]),
                summary=row["summary"],
                details=json.loads(row["details_json"]),
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def create_decision(self, decision: Decision) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO decisions (id, run_id, action, rationale, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decision.id,
                    decision.run_id,
                    decision.action.value,
                    decision.rationale,
                    decision.created_at.isoformat(),
                ),
            )

    def create_event(self, event: Event) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO events (id, entity_type, entity_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.entity_type,
                    event.entity_id,
                    event.event_type,
                    json.dumps(event.payload, sort_keys=True),
                    event.created_at.isoformat(),
                ),
            )

    def create_promotion(self, promotion: PromotionRecord) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO promotions (id, task_id, run_id, status, summary, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    promotion.id,
                    promotion.task_id,
                    promotion.run_id,
                    promotion.status.value,
                    promotion.summary,
                    json.dumps(promotion.details, sort_keys=True),
                    promotion.created_at.isoformat(),
                ),
            )

    def list_promotions(self, task_id: str | None = None) -> list[PromotionRecord]:
        query = """
            SELECT id, task_id, run_id, status, summary, details_json, created_at
            FROM promotions
        """
        params: tuple[str, ...] = ()
        if task_id:
            query += " WHERE task_id = ?"
            params = (task_id,)
        query += " ORDER BY created_at"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            PromotionRecord(
                id=row["id"],
                task_id=row["task_id"],
                run_id=row["run_id"],
                status=PromotionStatus(row["status"]),
                summary=row["summary"],
                details=json.loads(row["details_json"]),
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def latest_promotion(self, task_id: str) -> PromotionRecord | None:
        rows = self.list_promotions(task_id)
        return rows[-1] if rows else None

    def find_follow_on_task(self, parent_task_id: str, source_run_id: str) -> Task | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                       external_ref_type, external_ref_id, strategy, max_attempts,
                       required_artifacts_json, status, created_at, updated_at
                FROM tasks
                WHERE parent_task_id = ? AND source_run_id = ?
                ORDER BY created_at
                LIMIT 1
                """,
                (parent_task_id, source_run_id),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def list_events(self, entity_type: str | None = None, entity_id: str | None = None) -> list[Event]:
        query = """
            SELECT id, entity_type, entity_id, event_type, payload_json, created_at
            FROM events
        """
        clauses: list[str] = []
        params: list[str] = []
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [
            Event(
                id=row["id"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def list_decisions(self, run_id: str) -> list[Decision]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, action, rationale, created_at
                FROM decisions WHERE run_id = ? ORDER BY created_at
                """,
                (run_id,),
            ).fetchall()
        return [
            Decision(
                id=row["id"],
                run_id=row["run_id"],
                action=DecisionAction(row["action"]),
                rationale=row["rationale"],
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def mark_run(self, run: Run, status: RunStatus, summary: str) -> Run:
        updated = replace(run, status=status, summary=summary, updated_at=datetime.now(UTC))
        self.update_run(updated)
        return updated

    def _task_from_row(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            objective=row["objective"],
            priority=int(row["priority"]),
            parent_task_id=row["parent_task_id"],
            source_run_id=row["source_run_id"],
            external_ref_type=row["external_ref_type"],
            external_ref_id=row["external_ref_id"],
            strategy=row["strategy"],
            max_attempts=int(row["max_attempts"]),
            required_artifacts=json.loads(row["required_artifacts_json"]),
            status=TaskStatus(row["status"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def _run_from_row(self, row: sqlite3.Row) -> Run:
        return Run(
            id=row["id"],
            task_id=row["task_id"],
            status=RunStatus(row["status"]),
            attempt=int(row["attempt"]),
            summary=row["summary"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def metrics_snapshot(self, project_id: str | None = None) -> dict[str, object]:
        task_filter = ""
        params: list[str] = []
        if project_id:
            task_filter = " WHERE project_id = ?"
            params.append(project_id)
        with self.connect() as connection:
            task_rows = connection.execute(
                f"""
                SELECT status, COUNT(*) AS count
                FROM tasks
                {task_filter}
                GROUP BY status
                """,
                tuple(params),
            ).fetchall()
            run_query = """
                SELECT COUNT(*) AS total_runs, COALESCE(AVG(attempt), 0) AS avg_attempt
                FROM runs
            """
            promotion_query = "SELECT status, COUNT(*) AS count FROM promotions"
            promotion_params: tuple[str, ...] = ()
            run_params: tuple[str, ...] = ()
            if project_id:
                run_query = """
                    SELECT COUNT(*) AS total_runs, COALESCE(AVG(r.attempt), 0) AS avg_attempt
                    FROM runs r
                    JOIN tasks t ON t.id = r.task_id
                    WHERE t.project_id = ?
                """
                run_params = (project_id,)
                promotion_query = """
                    SELECT p.status, COUNT(*) AS count
                    FROM promotions p
                    JOIN tasks t ON t.id = p.task_id
                    WHERE t.project_id = ?
                    GROUP BY p.status
                """
                promotion_params = (project_id,)
            else:
                promotion_query += " GROUP BY status"
            run_row = connection.execute(run_query, run_params).fetchone()
            promotion_rows = connection.execute(promotion_query, promotion_params).fetchall()
        tasks_by_status = {row["status"]: int(row["count"]) for row in task_rows}
        promotions_by_status = {row["status"]: int(row["count"]) for row in promotion_rows}
        return {
            "project_id": project_id,
            "tasks_by_status": tasks_by_status,
            "total_runs": int(run_row["total_runs"]),
            "average_attempt": float(run_row["avg_attempt"]),
            "active_leases": len(self.list_task_leases()),
            "promotions_by_status": promotions_by_status,
        }
