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
    Migration(
        version=10,
        name="task_validation_modes",
        sql="""
        ALTER TABLE tasks ADD COLUMN validation_mode TEXT NOT NULL DEFAULT 'default_focused';
        UPDATE tasks
        SET validation_mode = 'lightweight_repair'
        WHERE validation_mode = 'default_focused'
          AND strategy IN ('executor_repair', 'timeout_decomposition', 'bounded_unblocker', 'deterministic_reliability');
        """,
    ),
    Migration(
        version=11,
        name="context_manager",
        sql="""
        CREATE TABLE IF NOT EXISTS objectives (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 100,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS intent_models (
            id TEXT PRIMARY KEY,
            objective_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            intent_summary TEXT NOT NULL,
            success_definition TEXT NOT NULL DEFAULT '',
            non_negotiables_json TEXT NOT NULL DEFAULT '[]',
            preferred_tradeoffs_json TEXT NOT NULL DEFAULT '[]',
            unacceptable_outcomes_json TEXT NOT NULL DEFAULT '[]',
            known_unknowns_json TEXT NOT NULL DEFAULT '[]',
            operator_examples_json TEXT NOT NULL DEFAULT '[]',
            frustration_signals_json TEXT NOT NULL DEFAULT '[]',
            sop_constraints_json TEXT NOT NULL DEFAULT '[]',
            current_confidence REAL NOT NULL DEFAULT 0.0,
            author_type TEXT NOT NULL DEFAULT 'operator',
            created_at TEXT NOT NULL,
            FOREIGN KEY(objective_id) REFERENCES objectives(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intent_models_objective_version
        ON intent_models(objective_id, version);

        CREATE TABLE IF NOT EXISTS mermaid_artifacts (
            id TEXT PRIMARY KEY,
            objective_id TEXT NOT NULL,
            diagram_type TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            content TEXT NOT NULL,
            required_for_execution INTEGER NOT NULL DEFAULT 0,
            blocking_reason TEXT NOT NULL DEFAULT '',
            author_type TEXT NOT NULL DEFAULT 'operator',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(objective_id) REFERENCES objectives(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_mermaid_artifacts_objective_type_version
        ON mermaid_artifacts(objective_id, diagram_type, version);

        CREATE TABLE IF NOT EXISTS context_records (
            id TEXT PRIMARY KEY,
            record_type TEXT NOT NULL,
            project_id TEXT NOT NULL,
            objective_id TEXT,
            task_id TEXT,
            run_id TEXT,
            visibility TEXT NOT NULL DEFAULT 'model_visible',
            author_type TEXT NOT NULL DEFAULT 'system',
            author_id TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id),
            FOREIGN KEY(objective_id) REFERENCES objectives(id),
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(run_id) REFERENCES runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_context_records_project_created
        ON context_records(project_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_context_records_objective_created
        ON context_records(objective_id, created_at);
        """,
    ),
    Migration(
        version=12,
        name="task_objective_linkage",
        sql="""
        ALTER TABLE tasks ADD COLUMN objective_id TEXT REFERENCES objectives(id);
        CREATE INDEX IF NOT EXISTS idx_tasks_objective_id ON tasks(objective_id);
        """,
    ),
    Migration(
        version=13,
        name="task_attempt_metadata",
        sql="""
        ALTER TABLE tasks ADD COLUMN attempt_metadata_json TEXT NOT NULL DEFAULT '{}';
        """,
    ),
    Migration(
        version=14,
        name="routing_outcome_history",
        sql="""
        CREATE TABLE IF NOT EXISTS routing_outcome_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            model_id TEXT NOT NULL,
            success INTEGER NOT NULL,
            llm_cost_usd REAL NOT NULL DEFAULT 0.0,
            llm_total_tokens REAL NOT NULL DEFAULT 0.0,
            llm_latency_ms REAL NOT NULL DEFAULT 0.0,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_routing_outcome_history_recorded_at
        ON routing_outcome_history(recorded_at);
        """,
    ),
    Migration(
        version=15,
        name="failure_patterns",
        sql="""
        CREATE TABLE IF NOT EXISTS failure_patterns (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            objective_id TEXT,
            attempt INTEGER NOT NULL DEFAULT 1,
            category TEXT NOT NULL,
            fingerprint TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(run_id) REFERENCES runs(id),
            FOREIGN KEY(objective_id) REFERENCES objectives(id)
        );
        CREATE INDEX IF NOT EXISTS idx_failure_patterns_task_created
        ON failure_patterns(task_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_failure_patterns_run_created
        ON failure_patterns(run_id, created_at);
        """,
    ),
    Migration(
        version=16,
        name="control_plane_v1",
        sql="""
        CREATE TABLE IF NOT EXISTS control_system_state (
            id TEXT PRIMARY KEY,
            global_state TEXT NOT NULL,
            master_switch INTEGER NOT NULL,
            freeze_reason TEXT,
            updated_at TEXT NOT NULL
        );

        INSERT OR IGNORE INTO control_system_state (id, global_state, master_switch, freeze_reason, updated_at)
        VALUES ('system', 'off', 0, NULL, CURRENT_TIMESTAMP);

        CREATE TABLE IF NOT EXISTS control_lane_state (
            lane_name TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            reason TEXT,
            cooldown_until TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS control_events (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            producer TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_control_events_created_at
        ON control_events(created_at);
        CREATE INDEX IF NOT EXISTS idx_control_events_entity
        ON control_events(entity_type, entity_id, created_at);

        CREATE TABLE IF NOT EXISTS control_cooldowns (
            id TEXT PRIMARY KEY,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            until_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_control_cooldowns_scope
        ON control_cooldowns(scope_type, scope_id, until_at);

        CREATE TABLE IF NOT EXISTS control_budgets (
            id TEXT PRIMARY KEY,
            budget_scope TEXT NOT NULL,
            budget_key TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            usage_count INTEGER NOT NULL,
            usage_cost_usd REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_control_budgets_scope
        ON control_budgets(budget_scope, budget_key, window_start, window_end);

        CREATE TABLE IF NOT EXISTS control_worker_runs (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            objective_id TEXT,
            worker_kind TEXT NOT NULL,
            runtime_name TEXT NOT NULL,
            model_name TEXT,
            attempt INTEGER NOT NULL,
            status TEXT NOT NULL,
            classification TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            breadcrumb_path TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_control_worker_runs_task
        ON control_worker_runs(task_id, started_at);

        CREATE TABLE IF NOT EXISTS control_breadcrumb_index (
            id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            worker_run_id TEXT,
            classification TEXT,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_control_breadcrumb_index_entity
        ON control_breadcrumb_index(entity_type, entity_id, created_at);

        CREATE TABLE IF NOT EXISTS control_recovery_actions (
            id TEXT PRIMARY KEY,
            action_type TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_control_recovery_actions_target
        ON control_recovery_actions(target_type, target_id, created_at);
        """,
    ),
    Migration(
        version=17,
        name="validation_queue",
        sql="""
        CREATE TABLE IF NOT EXISTS validation_queue (
            id TEXT PRIMARY KEY,
            run_id TEXT,
            task_id TEXT,
            snapshot_id TEXT,
            priority INTEGER,
            created_at TEXT,
            status TEXT DEFAULT 'pending',
            started_at TEXT,
            completed_at TEXT
        );
        """,
    ),
    Migration(
        version=18,
        name="decision_queue",
        sql="""
        CREATE TABLE IF NOT EXISTS decision_queue (
            id TEXT PRIMARY KEY,
            run_id TEXT,
            task_id TEXT,
            evaluation_id TEXT,
            priority INTEGER,
            created_at TEXT,
            status TEXT DEFAULT 'pending',
            started_at TEXT,
            completed_at TEXT
        );
        """,
    ),
    Migration(
        version=19,
        name="plans_and_task_plan_linkage",
        sql="""
        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            objective_id TEXT NOT NULL,
            parent_plan_id TEXT,
            mermaid_node_id TEXT,
            plan_revision INTEGER NOT NULL DEFAULT 1,
            slice_json TEXT NOT NULL DEFAULT '{}',
            atomicity_assessment_json TEXT NOT NULL DEFAULT '{}',
            approval_status TEXT NOT NULL DEFAULT 'approved',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(objective_id) REFERENCES objectives(id)
        );
        CREATE INDEX IF NOT EXISTS idx_plans_objective ON plans(objective_id);
        CREATE INDEX IF NOT EXISTS idx_plans_node ON plans(objective_id, mermaid_node_id);

        ALTER TABLE tasks ADD COLUMN plan_id TEXT;
        ALTER TABLE tasks ADD COLUMN mermaid_node_id TEXT;
        CREATE INDEX IF NOT EXISTS idx_tasks_plan ON tasks(plan_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_objective_node ON tasks(objective_id, mermaid_node_id);
        """,
    ),
    Migration(
        version=20,
        name="objective_phase_column",
        sql="""
        ALTER TABLE objectives ADD COLUMN phase TEXT NOT NULL DEFAULT 'created';
        """,
    ),
]


def apply_migrations(connection: sqlite3.Connection) -> list[int]:
    connection.execute("BEGIN IMMEDIATE")
    try:
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
            # Serialize migration application so concurrent harness processes do
            # not race between the version check and the INSERT record.
            for statement in migration.sql.split(";"):
                statement = statement.strip()
                if statement:
                    connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            newly_applied.append(migration.version)
        connection.commit()
        return newly_applied
    except Exception:
        connection.rollback()
        raise
