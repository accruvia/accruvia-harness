from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .domain import Run, RunStatus
from .migrations import MIGRATIONS, apply_migrations
from .persistence.events_metrics import EventsMetricsStoreMixin
from .persistence.project_task import ProjectTaskStoreMixin
from .persistence.run_records import RunRecordsStoreMixin


class SQLiteHarnessStore(ProjectTaskStoreMixin, RunRecordsStoreMixin, EventsMetricsStoreMixin):
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

    def mark_run(self, run: Run, status: RunStatus, summary: str) -> Run:
        updated = replace(run, status=status, summary=summary, updated_at=datetime.now(UTC))
        self.update_run(updated)
        return updated
