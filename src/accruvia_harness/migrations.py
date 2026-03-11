from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="initial_schema",
        sql="""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            title TEXT NOT NULL,
            objective TEXT NOT NULL,
            priority INTEGER NOT NULL,
            external_ref_type TEXT,
            external_ref_id TEXT,
            strategy TEXT NOT NULL,
            max_attempts INTEGER NOT NULL,
            required_artifacts_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        );

        CREATE TABLE IF NOT EXISTS evaluations (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            verdict TEXT NOT NULL,
            confidence REAL NOT NULL,
            summary TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        );

        CREATE TABLE IF NOT EXISTS decisions (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            action TEXT NOT NULL,
            rationale TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        );

        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """,
    ),
    Migration(
        version=2,
        name="task_lineage_and_leases",
        sql="""
        ALTER TABLE tasks ADD COLUMN parent_task_id TEXT;
        ALTER TABLE tasks ADD COLUMN source_run_id TEXT;

        CREATE TABLE IF NOT EXISTS task_leases (
            task_id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        """,
    ),
    Migration(
        version=3,
        name="promotion_records",
        sql="""
        CREATE TABLE IF NOT EXISTS promotions (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(run_id) REFERENCES runs(id)
        );
        """,
    ),
    Migration(
        version=4,
        name="task_validation_profiles",
        sql="""
        ALTER TABLE tasks ADD COLUMN validation_profile TEXT NOT NULL DEFAULT 'generic';
        """,
    ),
    Migration(
        version=5,
        name="project_adapters",
        sql="""
        ALTER TABLE projects ADD COLUMN adapter_name TEXT NOT NULL DEFAULT 'generic';
        """,
    ),
    Migration(
        version=6,
        name="task_external_ref_metadata",
        sql="""
        ALTER TABLE tasks ADD COLUMN external_ref_metadata_json TEXT NOT NULL DEFAULT '{}';
        """,
    ),
    Migration(
        version=7,
        name="parallel_execution",
        sql="""
        ALTER TABLE projects ADD COLUMN max_concurrent_tasks INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE tasks ADD COLUMN max_branches INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE runs ADD COLUMN branch_id TEXT;
        """,
    ),
    Migration(
        version=8,
        name="task_scope",
        sql="""
        ALTER TABLE tasks ADD COLUMN scope_json TEXT NOT NULL DEFAULT '{}';
        """,
    ),
    Migration(
        version=9,
        name="project_workspace_and_promotion_policy",
        sql="""
        ALTER TABLE projects ADD COLUMN workspace_policy TEXT NOT NULL DEFAULT 'isolated_required';
        ALTER TABLE projects ADD COLUMN promotion_mode TEXT NOT NULL DEFAULT 'branch_and_pr';
        ALTER TABLE projects ADD COLUMN repo_provider TEXT;
        ALTER TABLE projects ADD COLUMN repo_name TEXT;
        ALTER TABLE projects ADD COLUMN base_branch TEXT NOT NULL DEFAULT 'main';
        """,
    ),
]


def apply_migrations(connection: sqlite3.Connection) -> list[int]:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    rows = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    applied = {int(row[0]) for row in rows}
    newly_applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        # Execute each statement individually to stay within the connection's
        # transaction instead of using executescript() which auto-commits and
        # can leave partially-applied migrations unrecorded.
        for statement in migration.sql.split(";"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (migration.version, migration.name),
        )
        newly_applied.append(migration.version)
    return newly_applied
