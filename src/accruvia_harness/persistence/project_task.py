from __future__ import annotations

import json
from datetime import UTC, datetime

from .common import project_from_row, task_from_row, task_lease_from_row
from ..domain import Project, Task, TaskLease, TaskStatus


class ProjectTaskStoreMixin:
    def create_project(self, project: Project) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO projects (id, name, description, adapter_name, created_at) VALUES (?, ?, ?, ?, ?)",
                (project.id, project.name, project.description, project.adapter_name, project.created_at.isoformat()),
            )

    def list_projects(self) -> list[Project]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, description, adapter_name, created_at FROM projects ORDER BY created_at"
            ).fetchall()
        return [project_from_row(row) for row in rows]

    def get_project(self, project_id: str) -> Project | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, name, description, adapter_name, created_at FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        return project_from_row(row) if row else None

    def create_task(self, task: Task) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, objective, priority, parent_task_id, source_run_id,
                    external_ref_type, external_ref_id,
                    validation_profile, strategy, max_attempts, required_artifacts_json, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    task.validation_profile,
                    task.strategy,
                    task.max_attempts,
                    json.dumps(task.required_artifacts, sort_keys=True),
                    task.status.value,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )

    def list_tasks(self, project_id: str | None = None) -> list[Task]:
        query = """
            SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                   external_ref_type, external_ref_id, validation_profile,
                   strategy, max_attempts, required_artifacts_json, status, created_at, updated_at
            FROM tasks
        """
        params: tuple[str, ...] = ()
        if project_id:
            query += " WHERE project_id = ?"
            params = (project_id,)
        query += " ORDER BY priority DESC, created_at"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [task_from_row(row) for row in rows]

    def get_task(self, task_id: str) -> Task | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                       external_ref_type, external_ref_id, validation_profile, strategy, max_attempts,
                       required_artifacts_json, status, created_at, updated_at
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
        return task_from_row(row) if row else None

    def get_task_by_external_ref(self, ref_type: str, ref_id: str) -> Task | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                       external_ref_type, external_ref_id, validation_profile, strategy, max_attempts,
                       required_artifacts_json, status, created_at, updated_at
                FROM tasks
                WHERE external_ref_type = ? AND external_ref_id = ?
                ORDER BY created_at
                LIMIT 1
                """,
                (ref_type, ref_id),
            ).fetchone()
        return task_from_row(row) if row else None

    def next_pending_task(self, project_id: str | None = None) -> Task | None:
        query = """
            SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                   external_ref_type, external_ref_id, validation_profile, strategy, max_attempts,
                   required_artifacts_json, status, created_at, updated_at
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
        return task_from_row(row) if row else None

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, datetime.now(UTC).isoformat(), task_id),
            )

    def acquire_task_lease(self, worker_id: str, lease_seconds: int, project_id: str | None = None) -> Task | None:
        now = datetime.now(UTC)
        expires_at = datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=UTC)
        with self.connect() as connection:
            connection.execute("DELETE FROM task_leases WHERE lease_expires_at <= ?", (now.isoformat(),))
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
                "SELECT task_id, worker_id, lease_expires_at, created_at FROM task_leases ORDER BY created_at"
            ).fetchall()
        return [task_lease_from_row(row) for row in rows]

    def find_follow_on_task(self, parent_task_id: str, source_run_id: str) -> Task | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                       external_ref_type, external_ref_id, validation_profile, strategy, max_attempts,
                       required_artifacts_json, status, created_at, updated_at
                FROM tasks
                WHERE parent_task_id = ? AND source_run_id = ?
                ORDER BY created_at
                LIMIT 1
                """,
                (parent_task_id, source_run_id),
            ).fetchone()
        return task_from_row(row) if row else None

    def list_child_tasks(self, parent_task_id: str) -> list[Task]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                       external_ref_type, external_ref_id, validation_profile, strategy, max_attempts,
                       required_artifacts_json, status, created_at, updated_at
                FROM tasks
                WHERE parent_task_id = ?
                ORDER BY created_at
                """,
                (parent_task_id,),
            ).fetchall()
        return [task_from_row(row) for row in rows]
