from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from .common import project_from_row, task_from_row, task_lease_from_row
from ..domain import ObjectiveStatus, Project, Task, TaskLease, TaskStatus, validate_task_transition


def _workflow_state_disposition(metadata: dict[str, object]) -> dict[str, object] | None:
    disposition = metadata.get("workflow_state_disposition") if isinstance(metadata, dict) else None
    return disposition if isinstance(disposition, dict) else None


def _task_ignored_for_objective_phase(status: TaskStatus, metadata: dict[str, object]) -> bool:
    workflow_disposition = _workflow_state_disposition(metadata)
    if workflow_disposition and str(workflow_disposition.get("kind") or "").strip() == "ignore_obsolete":
        return True
    disposition = metadata.get("failed_task_disposition") if isinstance(metadata, dict) else None
    return bool(
        status == TaskStatus.FAILED
        and isinstance(disposition, dict)
        and str(disposition.get("kind") or "").strip() == "waive_obsolete"
    )


def _task_superseded_by_completed_peer(
    status: TaskStatus,
    external_ref_type: str | None,
    external_ref_id: str | None,
    metadata: dict[str, object],
    completed_external_refs: set[tuple[str, str]],
    completed_review_dimensions: set[str],
) -> bool:
    if status != TaskStatus.FAILED:
        return False
    normalized_type = str(external_ref_type or "").strip()
    normalized_id = str(external_ref_id or "").strip()
    if not normalized_type:
        return False
    # Objective-review remediation tasks are keyed by review finding. If a later
    # retry fails after an earlier sibling already completed for the same
    # finding or review dimension, the failed duplicate should not keep the
    # whole objective paused.
    remediation = metadata.get("objective_review_remediation") if isinstance(metadata, dict) else None
    dimension = ""
    if isinstance(remediation, dict):
        dimension = str(remediation.get("dimension") or "").strip()
    return (
        normalized_type == "objective_review"
        and (
            (normalized_id and (normalized_type, normalized_id) in completed_external_refs)
            or (dimension and dimension in completed_review_dimensions)
        )
    )


class ProjectTaskStoreMixin:
    def create_project(self, project: Project) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO projects (
                    id, name, description, adapter_name, workspace_policy, promotion_mode,
                    repo_provider, repo_name, base_branch, max_concurrent_tasks, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project.id,
                    project.name,
                    project.description,
                    project.adapter_name,
                    project.workspace_policy.value,
                    project.promotion_mode.value,
                    project.repo_provider.value if project.repo_provider is not None else None,
                    project.repo_name,
                    project.base_branch,
                    project.max_concurrent_tasks,
                    project.created_at.isoformat(),
                ),
            )

    def update_project(self, project: Project) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE projects
                SET name = ?, description = ?, adapter_name = ?, workspace_policy = ?, promotion_mode = ?,
                    repo_provider = ?, repo_name = ?, base_branch = ?, max_concurrent_tasks = ?
                WHERE id = ?
                """,
                (
                    project.name,
                    project.description,
                    project.adapter_name,
                    project.workspace_policy.value,
                    project.promotion_mode.value,
                    project.repo_provider.value if project.repo_provider is not None else None,
                    project.repo_name,
                    project.base_branch,
                    project.max_concurrent_tasks,
                    project.id,
                ),
            )

    def list_projects(self) -> list[Project]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, description, adapter_name, workspace_policy, promotion_mode,
                       repo_provider, repo_name, base_branch, max_concurrent_tasks, created_at
                FROM projects ORDER BY created_at
                """
            ).fetchall()
        return [project_from_row(row) for row in rows]

    def resolve_project(self, ref: str) -> Project | None:
        """Look up a project by ID or by name (case-insensitive)."""
        project = self.get_project(ref)
        if project is not None:
            return project
        for p in self.list_projects():
            if p.name.lower() == ref.lower():
                return p
        return None

    def get_project(self, project_id: str) -> Project | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, description, adapter_name, workspace_policy, promotion_mode,
                       repo_provider, repo_name, base_branch, max_concurrent_tasks, created_at
                FROM projects WHERE id = ?
                """,
                (project_id,),
            ).fetchone()
        return project_from_row(row) if row else None

    def create_task(self, task: Task) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, project_id, objective_id, title, objective, priority, parent_task_id, source_run_id,
                    external_ref_type, external_ref_id, external_ref_metadata_json,
                    validation_profile, validation_mode, scope_json, strategy, max_attempts, max_branches,
                    required_artifacts_json, attempt_metadata_json, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.project_id,
                    task.objective_id,
                    task.title,
                    task.objective,
                    task.priority,
                    task.parent_task_id,
                    task.source_run_id,
                    task.external_ref_type,
                    task.external_ref_id,
                    json.dumps(task.external_ref_metadata, sort_keys=True),
                    task.validation_profile,
                    task.validation_mode,
                    json.dumps(task.scope, sort_keys=True),
                    task.strategy,
                    task.max_attempts,
                    task.max_branches,
                    json.dumps(task.required_artifacts, sort_keys=True),
                    json.dumps(task.attempt_metadata, sort_keys=True),
                    task.status.value,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )

    def list_tasks(self, project_id: str | None = None) -> list[Task]:
        query = """
            SELECT id, project_id, title, objective, priority, parent_task_id, source_run_id,
                   objective_id,
                   external_ref_type, external_ref_id, external_ref_metadata_json, validation_profile, validation_mode, scope_json,
                   strategy, max_attempts, max_branches, required_artifacts_json, attempt_metadata_json, status, created_at, updated_at
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
                SELECT id, project_id, objective_id, title, objective, priority, parent_task_id, source_run_id,
                       external_ref_type, external_ref_id, external_ref_metadata_json, validation_profile, validation_mode, scope_json,
                       strategy, max_attempts, max_branches, required_artifacts_json, attempt_metadata_json, status, created_at, updated_at
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
        return task_from_row(row) if row else None

    def get_task_by_external_ref(self, ref_type: str, ref_id: str) -> Task | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, project_id, objective_id, title, objective, priority, parent_task_id, source_run_id,
                       external_ref_type, external_ref_id, external_ref_metadata_json, validation_profile, validation_mode, scope_json,
                       strategy, max_attempts, max_branches, required_artifacts_json, attempt_metadata_json, status, created_at, updated_at
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
                   external_ref_type, external_ref_id, external_ref_metadata_json, validation_profile, validation_mode, scope_json,
                   strategy, max_attempts, max_branches, required_artifacts_json, attempt_metadata_json, status, created_at, updated_at
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
        objective_id: str | None = None
        with self.connect() as connection:
            row = connection.execute("SELECT status, objective_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown task: {task_id}")
            current = TaskStatus(row["status"])
            objective_id = row["objective_id"]
            validate_task_transition(current, status)
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, datetime.now(UTC).isoformat(), task_id),
            )
        if objective_id:
            self.update_objective_phase(objective_id)

    def acquire_task_lease(
        self,
        worker_id: str,
        lease_seconds: int,
        project_id: str | None = None,
        exclude_task_ids: set[str] | None = None,
    ) -> Task | None:
        now = datetime.now(UTC)
        expires_at = datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=UTC)
        with self.connect() as connection:
            connection.execute("DELETE FROM task_leases WHERE lease_expires_at <= ?", (now.isoformat(),))
            query = """
                SELECT t.id, t.project_id
                FROM tasks t
                LEFT JOIN task_leases l ON l.task_id = t.id
                WHERE t.status = ? AND l.task_id IS NULL
            """
            params: list[str] = [TaskStatus.PENDING.value]
            if project_id:
                query += " AND t.project_id = ?"
                params.append(project_id)
            if exclude_task_ids:
                placeholders = ",".join("?" for _ in exclude_task_ids)
                query += f" AND t.id NOT IN ({placeholders})"
                params.extend(sorted(exclude_task_ids))
            query += " ORDER BY t.priority DESC, t.created_at"
            candidates = connection.execute(query, tuple(params)).fetchall()
            task_id: str | None = None
            for candidate in candidates:
                cand_project_id = candidate["project_id"]
                project_row = connection.execute(
                    "SELECT max_concurrent_tasks FROM projects WHERE id = ?",
                    (cand_project_id,),
                ).fetchone()
                max_concurrent = int(project_row["max_concurrent_tasks"]) if project_row else 0
                if max_concurrent > 0:
                    active_count = connection.execute(
                        """
                        SELECT COUNT(*) AS cnt FROM task_leases l
                        JOIN tasks t ON t.id = l.task_id
                        WHERE t.project_id = ?
                        """,
                        (cand_project_id,),
                    ).fetchone()["cnt"]
                    if active_count >= max_concurrent:
                        continue
                task_id = candidate["id"]
                break
            if task_id is None:
                return None
            # Use INSERT without OR REPLACE to fail if another worker grabbed it first
            try:
                connection.execute(
                    """
                    INSERT INTO task_leases (task_id, worker_id, lease_expires_at, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (task_id, worker_id, expires_at.isoformat(), now.isoformat()),
                )
            except sqlite3.IntegrityError:
                # Another worker acquired the lease between our read and write
                return None
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

    def list_task_leases(self, project_id: str | None = None) -> list[TaskLease]:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("DELETE FROM task_leases WHERE lease_expires_at <= ?", (now,))
            query = "SELECT l.task_id, l.worker_id, l.lease_expires_at, l.created_at FROM task_leases l"
            params: tuple[str, ...] = ()
            if project_id:
                query += " JOIN tasks t ON t.id = l.task_id WHERE t.project_id = ?"
                params = (project_id,)
            query += " ORDER BY l.created_at"
            rows = connection.execute(query, params).fetchall()
        return [task_lease_from_row(row) for row in rows]

    def find_follow_on_task(self, parent_task_id: str, source_run_id: str) -> Task | None:
        with self.connect() as connection:
            row = connection.execute(
                """
            SELECT id, project_id, objective_id, title, objective, priority, parent_task_id, source_run_id,
                   external_ref_type, external_ref_id, external_ref_metadata_json, validation_profile, validation_mode, scope_json,
                   strategy, max_attempts, max_branches, required_artifacts_json, attempt_metadata_json, status, created_at, updated_at
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
                       external_ref_type, external_ref_id, external_ref_metadata_json, validation_profile, validation_mode, scope_json,
                       strategy, max_attempts, max_branches, required_artifacts_json, attempt_metadata_json, status, created_at, updated_at
                FROM tasks
                WHERE parent_task_id = ?
                ORDER BY created_at
                """,
                (parent_task_id,),
            ).fetchall()
        return [task_from_row(row) for row in rows]

    def update_task_attempt_metadata(self, task_id: str, metadata: dict[str, object]) -> None:
        """Merge metadata into the existing attempt_metadata_json for the given task."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempt_metadata_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown task: {task_id}")
            existing = json.loads(row["attempt_metadata_json"]) if row["attempt_metadata_json"] else {}
            existing.update(metadata)
            connection.execute(
                "UPDATE tasks SET attempt_metadata_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(existing, sort_keys=True), datetime.now(UTC).isoformat(), task_id),
            )

    def update_objective_phase(self, objective_id: str) -> ObjectiveStatus | None:
        """Derive and persist the objective's phase from its linked tasks' statuses.

        Returns the new status, or ``None`` when the objective has no tasks
        (in which case the status is left unchanged).
        """
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, external_ref_type, external_ref_id, external_ref_metadata_json FROM tasks WHERE objective_id = ?",
                (objective_id,),
            ).fetchall()
            if not rows:
                return None
            completed_external_refs = {
                (str(row["external_ref_type"] or "").strip(), str(row["external_ref_id"] or "").strip())
                for row in rows
                if TaskStatus(row["status"]) == TaskStatus.COMPLETED
                and str(row["external_ref_type"] or "").strip()
                and str(row["external_ref_id"] or "").strip()
            }
            completed_review_dimensions = set()
            for row in rows:
                if TaskStatus(row["status"]) != TaskStatus.COMPLETED:
                    continue
                metadata = json.loads(row["external_ref_metadata_json"]) if row["external_ref_metadata_json"] else {}
                remediation = metadata.get("objective_review_remediation") if isinstance(metadata, dict) else None
                if isinstance(remediation, dict):
                    dimension = str(remediation.get("dimension") or "").strip()
                    if dimension:
                        completed_review_dimensions.add(dimension)
            effective_statuses: list[TaskStatus] = []
            for row in rows:
                status = TaskStatus(row["status"])
                metadata = json.loads(row["external_ref_metadata_json"]) if row["external_ref_metadata_json"] else {}
                if _task_ignored_for_objective_phase(status, metadata) or _task_superseded_by_completed_peer(
                    status,
                    row["external_ref_type"],
                    row["external_ref_id"],
                    metadata,
                    completed_external_refs,
                    completed_review_dimensions,
                ):
                    continue
                effective_statuses.append(status)
            if not effective_statuses:
                ignored_statuses = [TaskStatus(row["status"]) for row in rows]
                if any(status == TaskStatus.ACTIVE for status in ignored_statuses):
                    phase = ObjectiveStatus.EXECUTING
                elif any(status == TaskStatus.PENDING for status in ignored_statuses):
                    phase = ObjectiveStatus.PLANNING
                else:
                    # All linked tasks were explicitly ignored for phase
                    # purposes and only terminal work remains, so the
                    # objective is effectively resolved.
                    phase = ObjectiveStatus.RESOLVED
            elif any(s == TaskStatus.ACTIVE for s in effective_statuses):
                phase = ObjectiveStatus.EXECUTING
            elif all(s == TaskStatus.COMPLETED for s in effective_statuses):
                phase = ObjectiveStatus.RESOLVED
            elif any(s == TaskStatus.PENDING for s in effective_statuses):
                phase = ObjectiveStatus.PLANNING
            else:
                # All tasks failed (or mix of completed + failed) — pause
                phase = ObjectiveStatus.PAUSED
            connection.execute(
                "UPDATE objectives SET status = ?, updated_at = ? WHERE id = ?",
                (phase.value, datetime.now(UTC).isoformat(), objective_id),
            )
        return phase

    def update_task_external_metadata(self, task_id: str, metadata: dict[str, object]) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE tasks SET external_ref_metadata_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(metadata, sort_keys=True), datetime.now(UTC).isoformat(), task_id),
            )
