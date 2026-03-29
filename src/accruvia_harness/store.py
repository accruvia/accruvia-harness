from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .domain import Run, RunStatus, TaskStatus

logger = logging.getLogger(__name__)
from .migrations import MIGRATIONS, apply_migrations
from .persistence.control_plane import ControlPlaneStoreMixin
from .persistence.context_records import ContextRecordsStoreMixin
from .persistence.events_metrics import EventsMetricsStoreMixin
from .persistence.failure_patterns import FailurePatternsStoreMixin
from .persistence.project_task import ProjectTaskStoreMixin
from .persistence.run_records import RunRecordsStoreMixin


class SQLiteHarnessStore(
    ProjectTaskStoreMixin,
    RunRecordsStoreMixin,
    EventsMetricsStoreMixin,
    ContextRecordsStoreMixin,
    FailurePatternsStoreMixin,
    ControlPlaneStoreMixin,
):
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            apply_migrations(connection)
        self.ensure_control_lanes(["api", "harness", "worker", "watch", "telegram"])
        recovered = self.recover_stale_state()
        if any(v > 0 for v in recovered.values()):
            logger.warning("Startup recovery: %s", recovered)

    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
        return int(row["version"])

    def expected_schema_version(self) -> int:
        return max(migration.version for migration in MIGRATIONS)

    def recover_stale_state(self) -> dict[str, int]:
        """Find stuck runs/tasks from prior crashes and mark them failed."""
        now = datetime.now(UTC).isoformat()
        recovered: dict[str, int] = {"runs": 0, "tasks": 0, "leases": 0}
        with self.connect() as connection:
            # Expire stale leases
            expired = connection.execute(
                "DELETE FROM task_leases WHERE lease_expires_at <= ?", (now,)
            ).rowcount
            recovered["leases"] = expired

            # Mark in-progress runs (not terminal) as failed
            in_progress_statuses = [
                RunStatus.PLANNING.value,
                RunStatus.WORKING.value,
                RunStatus.VALIDATING.value,
                RunStatus.ANALYZING.value,
                RunStatus.DECIDING.value,
            ]
            placeholders = ",".join("?" for _ in in_progress_statuses)
            rows = connection.execute(
                f"""
                UPDATE runs
                SET status = ?, summary = 'Recovered: process crash detected', updated_at = ?
                WHERE status IN ({placeholders})
                  AND task_id NOT IN (
                      SELECT task_id
                      FROM task_leases
                      WHERE lease_expires_at > ?
                  )
                """,
                (RunStatus.FAILED.value, now, *in_progress_statuses, now),
            ).rowcount
            recovered["runs"] = rows

            # Reset ACTIVE tasks with no active lease back to PENDING
            tasks_reset = connection.execute(
                """
                UPDATE tasks SET status = ?, updated_at = ?
                WHERE status = ?
                AND id NOT IN (SELECT task_id FROM task_leases)
                """,
                (TaskStatus.PENDING.value, now, TaskStatus.ACTIVE.value),
            ).rowcount
            recovered["tasks"] = tasks_reset
        return recovered

    def mark_run(self, run: Run, status: RunStatus, summary: str) -> Run:
        updated = replace(run, status=status, summary=summary, updated_at=datetime.now(UTC))
        self.update_run(updated)
        return updated
