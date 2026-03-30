from __future__ import annotations

import json
from datetime import UTC, datetime

from .common import (
    control_breadcrumb_from_row,
    control_budget_from_row,
    control_cooldown_from_row,
    control_event_from_row,
    control_lane_state_from_row,
    control_recovery_action_from_row,
    control_system_state_from_row,
    control_worker_run_from_row,
)
from ..domain import (
    ControlBreadcrumb,
    ControlBudget,
    ControlCooldown,
    ControlEvent,
    ControlLaneState,
    ControlLaneStateValue,
    ControlRecoveryAction,
    ControlSystemState,
    ControlWorkerRun,
)


class ControlPlaneStoreMixin:
    def create_control_cooldown(self, cooldown: ControlCooldown) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO control_cooldowns (id, scope_type, scope_id, reason, until_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cooldown.id,
                    cooldown.scope_type,
                    cooldown.scope_id,
                    cooldown.reason,
                    cooldown.until_at.isoformat(),
                    cooldown.created_at.isoformat(),
                ),
            )

    def list_control_cooldowns(
        self,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        active_only: bool = False,
    ) -> list[ControlCooldown]:
        query = """
            SELECT id, scope_type, scope_id, reason, until_at, created_at
            FROM control_cooldowns
        """
        clauses: list[str] = []
        params: list[str] = []
        if scope_type:
            clauses.append("scope_type = ?")
            params.append(scope_type)
        if scope_id:
            clauses.append("scope_id = ?")
            params.append(scope_id)
        if active_only:
            clauses.append("until_at > ?")
            params.append(datetime.now(UTC).isoformat())
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [control_cooldown_from_row(row) for row in rows]

    def upsert_control_budget(self, budget: ControlBudget) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO control_budgets (
                    id, budget_scope, budget_key, window_start, window_end, usage_count, usage_cost_usd, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    budget_scope = excluded.budget_scope,
                    budget_key = excluded.budget_key,
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    usage_count = excluded.usage_count,
                    usage_cost_usd = excluded.usage_cost_usd,
                    updated_at = excluded.updated_at
                """,
                (
                    budget.id,
                    budget.budget_scope,
                    budget.budget_key,
                    budget.window_start.isoformat(),
                    budget.window_end.isoformat(),
                    budget.usage_count,
                    budget.usage_cost_usd,
                    budget.updated_at.isoformat(),
                ),
            )

    def get_control_budget(self, budget_scope: str, budget_key: str, window_start: datetime, window_end: datetime) -> ControlBudget | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, budget_scope, budget_key, window_start, window_end, usage_count, usage_cost_usd, updated_at
                FROM control_budgets
                WHERE budget_scope = ? AND budget_key = ? AND window_start = ? AND window_end = ?
                """,
                (budget_scope, budget_key, window_start.isoformat(), window_end.isoformat()),
            ).fetchone()
        return control_budget_from_row(row) if row else None

    def list_control_budgets(self, *, budget_scope: str | None = None, budget_key: str | None = None) -> list[ControlBudget]:
        query = """
            SELECT id, budget_scope, budget_key, window_start, window_end, usage_count, usage_cost_usd, updated_at
            FROM control_budgets
        """
        clauses: list[str] = []
        params: list[str] = []
        if budget_scope:
            clauses.append("budget_scope = ?")
            params.append(budget_scope)
        if budget_key:
            clauses.append("budget_key = ?")
            params.append(budget_key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [control_budget_from_row(row) for row in rows]

    def get_control_system_state(self) -> ControlSystemState:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, global_state, master_switch, freeze_reason, updated_at
                FROM control_system_state
                WHERE id = 'system'
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("Missing control_system_state bootstrap row")
        return control_system_state_from_row(row)

    def update_control_system_state(self, state: ControlSystemState) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO control_system_state (id, global_state, master_switch, freeze_reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    global_state = excluded.global_state,
                    master_switch = excluded.master_switch,
                    freeze_reason = excluded.freeze_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    state.id,
                    state.global_state.value,
                    int(state.master_switch),
                    state.freeze_reason,
                    state.updated_at.isoformat(),
                ),
            )

    def get_control_lane_state(self, lane_name: str) -> ControlLaneState | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT lane_name, state, reason, cooldown_until, updated_at
                FROM control_lane_state
                WHERE lane_name = ?
                """,
                (lane_name,),
            ).fetchone()
        return control_lane_state_from_row(row) if row else None

    def list_control_lane_states(self) -> list[ControlLaneState]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT lane_name, state, reason, cooldown_until, updated_at
                FROM control_lane_state
                ORDER BY lane_name
                """
            ).fetchall()
        return [control_lane_state_from_row(row) for row in rows]

    def update_control_lane_state(self, state: ControlLaneState) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO control_lane_state (lane_name, state, reason, cooldown_until, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lane_name) DO UPDATE SET
                    state = excluded.state,
                    reason = excluded.reason,
                    cooldown_until = excluded.cooldown_until,
                    updated_at = excluded.updated_at
                """,
                (
                    state.lane_name,
                    state.state.value,
                    state.reason,
                    state.cooldown_until.isoformat() if state.cooldown_until else None,
                    state.updated_at.isoformat(),
                ),
            )

    def ensure_control_lanes(self, lane_names: list[str]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            for lane_name in lane_names:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO control_lane_state (lane_name, state, reason, cooldown_until, updated_at)
                    VALUES (?, ?, NULL, NULL, ?)
                    """,
                    (lane_name, ControlLaneStateValue.PAUSED.value, now),
                )

    def create_control_event(self, event: ControlEvent) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO control_events (
                    id, event_type, entity_type, entity_id, producer, payload_json, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.event_type,
                    event.entity_type,
                    event.entity_id,
                    event.producer,
                    json.dumps(event.payload, sort_keys=True),
                    event.idempotency_key,
                    event.created_at.isoformat(),
                ),
            )

    def list_control_events(
        self,
        *,
        event_type: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int | None = None,
    ) -> list[ControlEvent]:
        query = """
            SELECT id, event_type, entity_type, entity_id, producer, payload_json, idempotency_key, created_at
            FROM control_events
        """
        clauses: list[str] = []
        params: list[str | int] = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [control_event_from_row(row) for row in rows]

    def create_control_breadcrumb(self, breadcrumb: ControlBreadcrumb) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO control_breadcrumb_index (
                    id, entity_type, entity_id, worker_run_id, classification, path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    breadcrumb.id,
                    breadcrumb.entity_type,
                    breadcrumb.entity_id,
                    breadcrumb.worker_run_id,
                    breadcrumb.classification,
                    breadcrumb.path,
                    breadcrumb.created_at.isoformat(),
                ),
            )

    def list_control_breadcrumbs(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> list[ControlBreadcrumb]:
        query = """
            SELECT id, entity_type, entity_id, worker_run_id, classification, path, created_at
            FROM control_breadcrumb_index
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
        query += " ORDER BY created_at DESC"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [control_breadcrumb_from_row(row) for row in rows]

    def create_control_recovery_action(self, action: ControlRecoveryAction) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO control_recovery_actions (
                    id, action_type, target_type, target_id, reason, result, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.id,
                    action.action_type,
                    action.target_type,
                    action.target_id,
                    action.reason,
                    action.result,
                    action.created_at.isoformat(),
                ),
            )

    def list_control_recovery_actions(self, *, target_type: str | None = None, target_id: str | None = None) -> list[ControlRecoveryAction]:
        query = """
            SELECT id, action_type, target_type, target_id, reason, result, created_at
            FROM control_recovery_actions
        """
        clauses: list[str] = []
        params: list[str] = []
        if target_type:
            clauses.append("target_type = ?")
            params.append(target_type)
        if target_id:
            clauses.append("target_id = ?")
            params.append(target_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [control_recovery_action_from_row(row) for row in rows]

    def upsert_control_worker_run(self, worker_run: ControlWorkerRun) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO control_worker_runs (
                    id, task_id, objective_id, worker_kind, runtime_name, model_name,
                    attempt, status, classification, started_at, ended_at, breadcrumb_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    task_id = excluded.task_id,
                    objective_id = excluded.objective_id,
                    worker_kind = excluded.worker_kind,
                    runtime_name = excluded.runtime_name,
                    model_name = excluded.model_name,
                    attempt = excluded.attempt,
                    status = excluded.status,
                    classification = excluded.classification,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    breadcrumb_path = excluded.breadcrumb_path
                """,
                (
                    worker_run.id,
                    worker_run.task_id,
                    worker_run.objective_id,
                    worker_run.worker_kind,
                    worker_run.runtime_name,
                    worker_run.model_name,
                    worker_run.attempt,
                    worker_run.status,
                    worker_run.classification,
                    worker_run.started_at.isoformat(),
                    worker_run.ended_at.isoformat() if worker_run.ended_at else None,
                    worker_run.breadcrumb_path,
                ),
            )

    def get_control_worker_run(self, worker_run_id: str) -> ControlWorkerRun | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, task_id, objective_id, worker_kind, runtime_name, model_name,
                       attempt, status, classification, started_at, ended_at, breadcrumb_path
                FROM control_worker_runs
                WHERE id = ?
                """,
                (worker_run_id,),
            ).fetchone()
        return control_worker_run_from_row(row) if row else None

    def list_control_worker_runs(self, task_id: str | None = None) -> list[ControlWorkerRun]:
        query = """
            SELECT id, task_id, objective_id, worker_kind, runtime_name, model_name,
                   attempt, status, classification, started_at, ended_at, breadcrumb_path
            FROM control_worker_runs
        """
        params: list[str] = []
        if task_id:
            query += " WHERE task_id = ?"
            params.append(task_id)
        query += " ORDER BY started_at DESC"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [control_worker_run_from_row(row) for row in rows]
