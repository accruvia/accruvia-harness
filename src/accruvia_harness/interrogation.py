from __future__ import annotations

from dataclasses import asdict

from .domain import serialize_dataclass
from .store import SQLiteHarnessStore
from .telemetry import TelemetrySink


class HarnessQueryService:
    def __init__(self, store: SQLiteHarnessStore, telemetry: TelemetrySink | None = None) -> None:
        self.store = store
        self.telemetry = telemetry

    def portfolio_summary(self) -> dict[str, object]:
        projects = self.store.list_projects()
        project_summaries = []
        for project in projects:
            metrics = self.store.metrics_snapshot(project.id)
            project_summaries.append(
                {
                    "project": serialize_dataclass(project),
                    "metrics": metrics,
                }
            )
        return {
            "project_count": len(project_summaries),
            "projects": project_summaries,
            "global_metrics": self.store.metrics_snapshot(),
        }

    def project_summary(self, project_id: str) -> dict[str, object]:
        tasks = self.store.list_tasks(project_id)
        runs = []
        for task in tasks:
            runs.extend(self.store.list_runs(task.id))
        return {
            "project_id": project_id,
            "metrics": self.store.metrics_snapshot(project_id),
            "tasks": [serialize_dataclass(task) for task in tasks],
            "recent_runs": [serialize_dataclass(run) for run in runs[-10:]],
            "recent_promotions": [
                serialize_dataclass(promotion) for task in tasks for promotion in self.store.list_promotions(task.id)
            ][-10:],
        }

    def task_report(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        runs = self.store.list_runs(task_id)
        run_reports = []
        for run in runs:
            run_reports.append(
                {
                    "run": serialize_dataclass(run),
                    "artifacts": [serialize_dataclass(item) for item in self.store.list_artifacts(run.id)],
                    "evaluations": [serialize_dataclass(item) for item in self.store.list_evaluations(run.id)],
                    "decisions": [serialize_dataclass(item) for item in self.store.list_decisions(run.id)],
                }
            )
        events = self.store.list_events("task", task_id)
        return {
            "task": serialize_dataclass(task),
            "runs": run_reports,
            "promotions": [serialize_dataclass(item) for item in self.store.list_promotions(task.id)],
            "events": [serialize_dataclass(event) for event in events],
            "lineage": self.task_lineage(task_id),
        }

    def task_lineage(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")

        ancestors = []
        current = task
        while current.parent_task_id:
            parent = self.store.get_task(current.parent_task_id)
            if parent is None:
                break
            ancestors.append(serialize_dataclass(parent))
            current = parent
        ancestors.reverse()

        def descendants(parent_id: str) -> list[dict[str, object]]:
            children = self.store.list_child_tasks(parent_id)
            payload = []
            for child in children:
                payload.append(
                    {
                        "task": serialize_dataclass(child),
                        "children": descendants(child.id),
                    }
                )
            return payload

        return {
            "ancestors": ancestors,
            "children": descendants(task.id),
        }

    def context_packet(self, project_id: str | None = None) -> dict[str, object]:
        metrics = self.store.metrics_snapshot(project_id)
        tasks = self.store.list_tasks(project_id)[:10] if project_id else self.store.list_tasks()[:10]
        focus_tasks = []
        for task in tasks:
            runs = self.store.list_runs(task.id)
            last_run = serialize_dataclass(runs[-1]) if runs else None
            focus_tasks.append(
                {
                    "task": serialize_dataclass(task),
                    "last_run": last_run,
                    "last_promotion": serialize_dataclass(self.store.latest_promotion(task.id))
                    if self.store.latest_promotion(task.id)
                    else None,
                }
            )
        return {
            "project_id": project_id,
            "metrics": metrics,
            "focus_tasks": focus_tasks,
            "leases": [asdict(lease) | {"lease_expires_at": lease.lease_expires_at.isoformat(), "created_at": lease.created_at.isoformat()} for lease in self.store.list_task_leases()],
        }

    def operations_report(self, project_id: str | None = None) -> dict[str, object]:
        metrics = self.store.metrics_snapshot(project_id)
        tasks = self.store.list_tasks(project_id)
        pending_affirmations = []
        for task in tasks:
            promotion = self.store.latest_promotion(task.id)
            if promotion and promotion.status.value == "pending":
                pending_affirmations.append(
                    {
                        "task": serialize_dataclass(task),
                        "promotion": serialize_dataclass(promotion),
                    }
                )
        return {
            "project_id": project_id,
            "metrics": metrics,
            "pending_affirmations": pending_affirmations,
        }

    def dashboard_report(self, project_id: str | None = None) -> dict[str, object]:
        operations = self.operations_report(project_id)
        telemetry = self.telemetry.summary() if self.telemetry is not None else {}
        tasks = self.store.list_tasks(project_id)
        runs = [run for task in tasks for run in self.store.list_runs(task.id)]
        blocked_runs = [serialize_dataclass(run) for run in runs if run.status.value == "blocked"][-10:]
        failed_runs = [serialize_dataclass(run) for run in runs if run.status.value == "failed"][-10:]
        return {
            "project_id": project_id,
            "operations": operations,
            "telemetry": telemetry,
            "recent_blocked_runs": blocked_runs,
            "recent_failed_runs": failed_runs,
            "dashboard": {
                "queue_depth": sum(operations["metrics"]["tasks_by_status"].values()),
                "pending_promotions": operations["metrics"]["pending_promotions"],
                "active_leases": operations["metrics"]["active_leases"],
                "retry_rate": operations["metrics"]["retry_rate"],
                "promotion_approval_rate": operations["metrics"]["promotion_approval_rate"],
                "llm_cost_usd": telemetry.get("cost_totals", {}).get("cost_usd", 0.0),
                "llm_total_tokens": telemetry.get("cost_totals", {}).get("total_tokens", 0.0),
                "slowest_operations_ms": telemetry.get("dashboard", {}).get("slowest_operations_ms", []),
            },
        }
