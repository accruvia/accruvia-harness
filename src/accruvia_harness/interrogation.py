from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .domain import Project, Run, RunStatus, Task, TaskStatus, new_id, serialize_dataclass
from .llm import LLMExecutionResult, LLMInvocation, LLMRouter
from .telemetry import TelemetrySink


class ReadOnlyStore:
    _ALLOWED_METHODS = {
        "get_task",
        "get_project",
        "latest_promotion",
        "list_artifacts",
        "list_child_tasks",
        "list_decisions",
        "list_evaluations",
        "list_events",
        "list_projects",
        "list_promotions",
        "list_runs",
        "list_task_leases",
        "list_tasks",
        "metrics_snapshot",
    }

    def __init__(self, store) -> None:
        self._store = store

    def __getattr__(self, name: str):
        if name not in self._ALLOWED_METHODS:
            raise AttributeError(f"Attribute '{name}' is not available on ReadOnlyStore")
        return getattr(self._store, name)


class HarnessQueryService:
    def __init__(self, store, telemetry: TelemetrySink | None = None) -> None:
        self.store = ReadOnlyStore(store)
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
            "loop_status": self._loop_status(project_id),
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
            "loop_status": self._loop_status(project_id),
            "leases": [
                asdict(lease)
                | {"lease_expires_at": lease.lease_expires_at.isoformat(), "created_at": lease.created_at.isoformat()}
                for lease in self.store.list_task_leases(project_id)
            ],
            "operator_nudges": self._operator_nudges(project_id),
            "strategy_history": self._strategy_history(project_id),
            "telemetry_summary": self._telemetry_summary(),
        }

    def _operator_nudges(self, project_id: str | None) -> list[dict[str, object]]:
        if project_id is None:
            return []
        events = self.store.list_events("project", project_id)
        nudges = [event for event in events if event.event_type == "operator_nudge"]
        return [
            {
                "created_at": event.created_at.isoformat(),
                "note": str((event.payload or {}).get("note") or ""),
                "author": str((event.payload or {}).get("author") or ""),
            }
            for event in nudges[-5:]
        ]

    def _strategy_history(self, project_id: str | None) -> dict[str, object]:
        if project_id is None:
            return {
                "recent_heartbeats": [],
                "heartbeat_count": 0,
                "tasks_created_from_heartbeats": 0,
                "tasks_skipped_from_heartbeats": 0,
            }
        events = self.store.list_events("project", project_id)
        heartbeat_events = [event for event in events if event.event_type == "heartbeat_completed"]
        recent_heartbeats = []
        tasks_created = 0
        tasks_skipped = 0
        for event in heartbeat_events[-5:]:
            payload = dict(event.payload or {})
            created = int(payload.get("created_task_count", 0) or 0)
            skipped = int(payload.get("skipped_task_count", 0) or 0)
            tasks_created += created
            tasks_skipped += skipped
            recent_heartbeats.append(
                {
                    "created_at": event.created_at.isoformat(),
                    "summary": str(payload.get("summary") or ""),
                    "adapter_name": str(payload.get("adapter_name") or ""),
                    "issue_creation_needed": bool(payload.get("issue_creation_needed", False)),
                    "proposed_task_count": int(payload.get("proposed_task_count", 0) or 0),
                    "created_task_count": created,
                    "skipped_task_count": skipped,
                    "next_heartbeat_seconds": payload.get("next_heartbeat_seconds"),
                }
            )
        return {
            "recent_heartbeats": recent_heartbeats,
            "heartbeat_count": len(heartbeat_events),
            "tasks_created_from_heartbeats": sum(
                int((event.payload or {}).get("created_task_count", 0) or 0)
                for event in heartbeat_events
            ),
            "tasks_skipped_from_heartbeats": sum(
                int((event.payload or {}).get("skipped_task_count", 0) or 0)
                for event in heartbeat_events
            ),
            "recent_window_tasks_created": tasks_created,
            "recent_window_tasks_skipped": tasks_skipped,
        }

    def _telemetry_summary(self) -> dict[str, object]:
        if self.telemetry is None:
            return {}
        summary = self.telemetry.summary()
        return {
            "metric_totals": dict(summary.get("metric_totals", {})),
            "span_counts": dict(summary.get("span_counts", {})),
            "cost_totals": dict(summary.get("cost_totals", {})),
            "slowest_operations_ms": list(summary.get("dashboard", {}).get("slowest_operations_ms", [])),
            "highest_volume_metrics": list(summary.get("dashboard", {}).get("highest_volume_metrics", [])),
            "recent_warnings": list(summary.get("warnings", []))[-5:],
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
            "loop_status": self._loop_status(project_id),
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
                "queue_depth": operations["metrics"]["tasks_by_status"].get("pending", 0)
                + operations["metrics"]["tasks_by_status"].get("active", 0),
                "total_tasks": sum(operations["metrics"]["tasks_by_status"].values()),
                "pending_promotions": operations["metrics"]["pending_promotions"],
                "active_leases": operations["metrics"]["active_leases"],
                "retry_rate": operations["metrics"]["retry_rate"],
                "promotion_approval_rate": operations["metrics"]["promotion_approval_rate"],
                "llm_cost_usd": telemetry.get("cost_totals", {}).get("cost_usd", 0.0),
                "llm_total_tokens": telemetry.get("cost_totals", {}).get("total_tokens", 0.0),
                "slowest_operations_ms": telemetry.get("dashboard", {}).get("slowest_operations_ms", []),
                "healthy_idle": operations["loop_status"]["healthy_idle"],
            },
        }

    def _loop_status(self, project_id: str | None) -> dict[str, object]:
        metrics = self.store.metrics_snapshot(project_id)
        tasks = self.store.list_tasks(project_id)
        runs = [run for task in tasks for run in self.store.list_runs(task.id)]
        now = datetime.now(UTC)
        queue_depth = int(metrics.get("tasks_by_status", {}).get("pending", 0)) + int(
            metrics.get("tasks_by_status", {}).get("active", 0)
        )
        active_leases = int(metrics.get("active_leases", 0) or 0)
        latest_completed_task = max(
            (task for task in tasks if task.status == TaskStatus.COMPLETED),
            key=lambda item: item.updated_at,
            default=None,
        )
        latest_failure_run = max(
            (run for run in runs if run.status in {RunStatus.BLOCKED, RunStatus.FAILED}),
            key=lambda item: item.updated_at,
            default=None,
        )
        latest_project_events = self.store.list_events("project", project_id) if project_id is not None else []
        latest_heartbeat = next(
            (event for event in reversed(latest_project_events) if event.event_type == "heartbeat_completed"),
            None,
        )
        latest_schedule = next(
            (event for event in reversed(latest_project_events) if event.event_type == "heartbeat_scheduled"),
            None,
        )
        heartbeat_interval_seconds = None
        next_heartbeat_due_at = None
        next_heartbeat_due_in_seconds = None
        heartbeat_schedule_source = None
        if latest_schedule is not None:
            heartbeat_interval_seconds = int((latest_schedule.payload or {}).get("interval_seconds", 0) or 0) or None
            heartbeat_schedule_source = str((latest_schedule.payload or {}).get("source") or "")
            if heartbeat_interval_seconds is not None:
                next_due = latest_schedule.created_at + timedelta(seconds=heartbeat_interval_seconds)
                next_heartbeat_due_at = next_due.isoformat()
                next_heartbeat_due_in_seconds = max(0.0, (next_due - now).total_seconds())
        healthy_idle = (
            queue_depth == 0
            and active_leases == 0
            and latest_completed_task is not None
            and (
                latest_failure_run is None
                or latest_failure_run.updated_at <= latest_completed_task.updated_at
            )
        )
        status = "active" if queue_depth > 0 or active_leases > 0 else "idle"
        if healthy_idle:
            status = "healthy_idle"
        return {
            "status": status,
            "healthy_idle": healthy_idle,
            "queue_depth": queue_depth,
            "active_leases": active_leases,
            "last_completed_task_id": latest_completed_task.id if latest_completed_task else None,
            "last_completed_task_title": latest_completed_task.title if latest_completed_task else None,
            "last_completed_at": latest_completed_task.updated_at.isoformat() if latest_completed_task else None,
            "last_failed_or_blocked_run_id": latest_failure_run.id if latest_failure_run else None,
            "last_failed_or_blocked_at": latest_failure_run.updated_at.isoformat() if latest_failure_run else None,
            "last_heartbeat_at": latest_heartbeat.created_at.isoformat() if latest_heartbeat else None,
            "last_heartbeat_summary": str((latest_heartbeat.payload or {}).get("summary") or "") if latest_heartbeat else "",
            "heartbeat_interval_seconds": heartbeat_interval_seconds,
            "heartbeat_schedule_source": heartbeat_schedule_source,
            "next_heartbeat_due_at": next_heartbeat_due_at,
            "next_heartbeat_due_in_seconds": next_heartbeat_due_in_seconds,
        }


class InterrogationService:
    def __init__(
        self,
        query_service: HarnessQueryService,
        workspace_root: Path,
        llm_router: LLMRouter | None,
        telemetry: TelemetrySink | None = None,
    ) -> None:
        self.query_service = query_service
        self.workspace_root = workspace_root
        self.llm_router = llm_router
        self.telemetry = telemetry

    def explain_system(self, project_id: str | None = None) -> dict[str, object]:
        packet = self.query_service.context_packet(project_id)
        return self._explain(
            subject_type="system",
            subject_id=project_id or "portfolio",
            payload=packet,
            title=f"System explanation for {project_id or 'portfolio'}",
        )

    def explain_task(self, task_id: str) -> dict[str, object]:
        packet = self.query_service.task_report(task_id)
        return self._explain(
            subject_type="task",
            subject_id=task_id,
            payload=packet,
            title=f"Task explanation for {task_id}",
        )

    def _explain(self, subject_type: str, subject_id: str, payload: dict[str, Any], title: str) -> dict[str, object]:
        if self.llm_router is None:
            raise ValueError("No LLM router configured for interrogation")
        run_dir = self.workspace_root / "interrogation" / f"{subject_type}_{subject_id}" / new_id("explain")
        run_dir.mkdir(parents=True, exist_ok=True)
        task = Task(
            id=new_id("interrogation_task"),
            project_id="interrogation",
            title=title,
            objective=f"Explain harness {subject_type} state from read-only evidence.",
            status=TaskStatus.COMPLETED,
        )
        run = Run(
            id=new_id("interrogation_run"),
            task_id=task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary=f"Read-only explanation for {subject_type} {subject_id}",
        )
        prompt = self._build_prompt(subject_type, payload)
        if self.telemetry is not None:
            with self.telemetry.timed(
                "interrogation_explain",
                subject_type=subject_type,
                subject_id=subject_id,
            ):
                result, backend = self._execute_llm(task, run, prompt, run_dir)
        else:
            result, backend = self._execute_llm(task, run, prompt, run_dir)
        explanation_path = run_dir / "explanation.json"
        explanation_path.write_text(
            json.dumps(
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "backend": backend,
                    "explanation": result.response_text,
                    "diagnostics": result.diagnostics,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "backend": backend,
            "explanation": result.response_text,
            "prompt_path": str(result.prompt_path),
            "response_path": str(result.response_path),
            "explanation_path": str(explanation_path),
            "diagnostics": result.diagnostics,
        }

    def _execute_llm(self, task: Task, run: Run, prompt: str, run_dir: Path):
        invocation = LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir)
        execute = getattr(self.llm_router, "execute", None)
        if execute is not None:
            return execute(invocation, telemetry=self.telemetry)
        executor, backend = self.llm_router.resolve()
        return executor.execute(invocation), backend

    def _build_prompt(self, subject_type: str, payload: dict[str, Any]) -> str:
        return (
            "You are a read-only observer of the accrivia-harness control plane.\n"
            "Explain the current system state, risks, bottlenecks, and recommended next actions.\n"
            "Do not assume unstated facts. Base your explanation only on the provided evidence.\n\n"
            f"Subject Type: {subject_type}\n"
            f"Evidence:\n{json.dumps(payload, indent=2, sort_keys=True)}\n"
        )
