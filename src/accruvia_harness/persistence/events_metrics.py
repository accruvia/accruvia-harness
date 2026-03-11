from __future__ import annotations

import json

from .common import event_from_row
from ..domain import Event


class EventsMetricsStoreMixin:
    observer_webhook_url: str | None = None

    def create_event(self, event: Event) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO events (id, entity_type, entity_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.entity_type,
                    event.entity_id,
                    event.event_type,
                    json.dumps(event.payload, sort_keys=True),
                    event.created_at.isoformat(),
                ),
            )
        if self.observer_webhook_url:
            from ..observer_hook import notify_observer
            notify_observer(self.observer_webhook_url, event.event_type, event.entity_type, event.entity_id, event.payload)

    def list_events(self, entity_type: str | None = None, entity_id: str | None = None) -> list[Event]:
        query = "SELECT id, entity_type, entity_id, event_type, payload_json, created_at FROM events"
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
        return [event_from_row(row) for row in rows]

    def metrics_snapshot(self, project_id: str | None = None) -> dict[str, object]:
        task_filter = ""
        params: list[str] = []
        if project_id:
            task_filter = " WHERE project_id = ?"
            params.append(project_id)
        with self.connect() as connection:
            task_rows = connection.execute(
                f"SELECT status, COUNT(*) AS count FROM tasks {task_filter} GROUP BY status",
                tuple(params),
            ).fetchall()
            profile_query = "SELECT validation_profile, COUNT(*) AS count FROM tasks"
            profile_params: tuple[str, ...] = ()
            if project_id:
                profile_query += " WHERE project_id = ?"
                profile_params = (project_id,)
            profile_query += " GROUP BY validation_profile"
            profile_rows = connection.execute(profile_query, profile_params).fetchall()
            run_query = """
                SELECT COUNT(*) AS total_runs,
                       COALESCE(AVG(attempt), 0) AS avg_attempt,
                       COALESCE(SUM(CASE WHEN attempt > 1 THEN 1 ELSE 0 END), 0) AS retried_runs
                FROM runs
            """
            promotion_query = "SELECT status, COUNT(*) AS count FROM promotions"
            promotion_params: tuple[str, ...] = ()
            run_params: tuple[str, ...] = ()
            if project_id:
                run_query = """
                    SELECT COUNT(*) AS total_runs,
                           COALESCE(AVG(r.attempt), 0) AS avg_attempt,
                           COALESCE(SUM(CASE WHEN r.attempt > 1 THEN 1 ELSE 0 END), 0) AS retried_runs
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
        tasks_by_validation_profile = {row["validation_profile"]: int(row["count"]) for row in profile_rows}
        promotions_by_status = {row["status"]: int(row["count"]) for row in promotion_rows}
        total_runs = int(run_row["total_runs"])
        retried_runs = int(run_row["retried_runs"])
        approved = promotions_by_status.get("approved", 0)
        rejected = promotions_by_status.get("rejected", 0)
        pending = promotions_by_status.get("pending", 0)
        total_promotions = approved + rejected
        follow_on_count = len([task for task in self.list_tasks(project_id) if task.parent_task_id is not None])
        return {
            "project_id": project_id,
            "tasks_by_status": tasks_by_status,
            "tasks_by_validation_profile": tasks_by_validation_profile,
            "total_runs": total_runs,
            "average_attempt": float(run_row["avg_attempt"]),
            "active_leases": len(self.list_task_leases()),
            "promotions_by_status": promotions_by_status,
            "pending_promotions": pending,
            "retry_rate": (retried_runs / total_runs) if total_runs else 0.0,
            "promotion_approval_rate": (approved / total_promotions) if total_promotions else 0.0,
            "follow_on_task_count": follow_on_count,
        }
