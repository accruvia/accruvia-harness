from __future__ import annotations

import json
import re
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

    def red_team_mermaid(
        self,
        path: str | Path,
        *,
        block_index: int = 0,
        include_llm: bool = True,
        model: str | None = None,
    ) -> dict[str, object]:
        target_path = Path(path).resolve()
        if not target_path.exists():
            raise ValueError(f"Mermaid source not found: {target_path}")
        text = target_path.read_text(encoding="utf-8")
        review = _review_mermaid_text(text, path=target_path, block_index=block_index)
        payload: dict[str, object] = {
            "path": str(target_path),
            "block_index": review["block_index"],
            "block_count": review["block_count"],
            "source_type": review["source_type"],
            "ready_for_human_review": review["ready_for_human_review"],
            "diagram": review["diagram"],
            "deterministic_review": {
                "checks": review["checks"],
                "findings": review["findings"],
            },
            "llm_review": {
                "enabled": False,
                "available": self.llm_router is not None,
                "skipped_reason": "disabled_by_flag" if not include_llm else "llm_router_unavailable",
                "findings": [],
            },
        }
        if include_llm and self.llm_router is not None:
            llm_review = self._llm_red_team_mermaid(
                target_path=target_path,
                review=review,
                model=model,
            )
            payload["llm_review"] = llm_review
            llm_findings = list(llm_review.get("findings") or [])
            if any(str(item.get("severity") or "").lower() in {"critical", "major"} for item in llm_findings):
                payload["ready_for_human_review"] = False
        return payload

    def red_team_mermaid_text(
        self,
        diagram_text: str,
        *,
        source_label: str = "inline_mermaid",
        include_llm: bool = True,
        model: str | None = None,
    ) -> dict[str, object]:
        review = _review_mermaid_text(
            diagram_text,
            path=Path(source_label),
            block_index=0,
            diagram_only=True,
        )
        payload: dict[str, object] = {
            "path": source_label,
            "block_index": 0,
            "block_count": 1,
            "source_type": "inline_mermaid",
            "ready_for_human_review": review["ready_for_human_review"],
            "diagram": review["diagram"],
            "deterministic_review": {
                "checks": review["checks"],
                "findings": review["findings"],
            },
            "llm_review": {
                "enabled": False,
                "available": self.llm_router is not None,
                "skipped_reason": "disabled_by_flag" if not include_llm else "llm_router_unavailable",
                "findings": [],
            },
        }
        if include_llm and self.llm_router is not None:
            llm_review = self._llm_red_team_mermaid_inline(
                source_label=source_label,
                review=review,
                model=model,
            )
            payload["llm_review"] = llm_review
            llm_findings = list(llm_review.get("findings") or [])
            if any(str(item.get("severity") or "").lower() in {"critical", "major"} for item in llm_findings):
                payload["ready_for_human_review"] = False
        return payload

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

    def _llm_red_team_mermaid(
        self,
        *,
        target_path: Path,
        review: dict[str, object],
        model: str | None,
    ) -> dict[str, object]:
        if self.llm_router is None:
            return {
                "enabled": False,
                "available": False,
                "skipped_reason": "llm_router_unavailable",
                "findings": [],
            }
        run_dir = self.workspace_root / "interrogation" / "mermaid_red_team" / target_path.stem / new_id("review")
        run_dir.mkdir(parents=True, exist_ok=True)
        task = Task(
            id=new_id("interrogation_task"),
            project_id="interrogation",
            title=f"Red-team Mermaid {target_path.name}",
            objective="Red-team a Mermaid diagram before human review.",
            status=TaskStatus.COMPLETED,
        )
        run = Run(
            id=new_id("interrogation_run"),
            task_id=task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary=f"Mermaid red-team review for {target_path}",
        )
        prompt = self._build_mermaid_red_team_prompt(target_path=target_path, review=review)
        invocation = LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir, model=model)
        execute = getattr(self.llm_router, "execute", None)
        if execute is not None:
            result, backend = execute(invocation, telemetry=self.telemetry)
        else:
            executor, backend = self.llm_router.resolve()
            result = executor.execute(invocation)
        parsed = _parse_red_team_llm_response(result.response_text)
        return {
            "enabled": True,
            "available": True,
            "backend": backend,
            "prompt_path": str(result.prompt_path),
            "response_path": str(result.response_path),
            "diagnostics": result.diagnostics,
            "summary": str(parsed.get("summary") or "").strip(),
            "ready_for_human_review": bool(parsed.get("ready_for_human_review", False)),
            "findings": list(parsed.get("findings") or []),
        }

    def _llm_red_team_mermaid_inline(
        self,
        *,
        source_label: str,
        review: dict[str, object],
        model: str | None,
    ) -> dict[str, object]:
        if self.llm_router is None:
            return {
                "enabled": False,
                "available": False,
                "skipped_reason": "llm_router_unavailable",
                "findings": [],
            }
        run_dir = self.workspace_root / "interrogation" / "mermaid_red_team" / "inline" / new_id("review")
        run_dir.mkdir(parents=True, exist_ok=True)
        task = Task(
            id=new_id("interrogation_task"),
            project_id="interrogation",
            title=f"Red-team Mermaid {source_label}",
            objective="Red-team a generated Mermaid diagram before human review.",
            status=TaskStatus.COMPLETED,
        )
        run = Run(
            id=new_id("interrogation_run"),
            task_id=task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary=f"Mermaid red-team review for {source_label}",
        )
        rubric_path = Path(__file__).resolve().parents[2] / "specs" / "mermaid-red-team.md"
        rubric_text = rubric_path.read_text(encoding="utf-8") if rubric_path.exists() else ""
        prompt = (
            "You are red-teaming a generated Mermaid control diagram before human review.\n"
            "Use the rubric below. Find structural flaws, intent drift, planning blockage, boundary blur, control ambiguity, ownership inflation, and implementer traps.\n"
            "The source is diagram-only, so do not require surrounding prose or an execution contract unless the missing contract is visible inside the diagram itself.\n"
            "Return JSON only with keys: summary, ready_for_human_review, findings.\n"
            "findings must be an array of objects with keys: severity, class, summary, rationale, patch_hint.\n\n"
            f"Rubric:\n{rubric_text}\n\n"
            f"Source label: {source_label}\n"
            f"Deterministic review:\n{json.dumps(review, indent=2, sort_keys=True)}\n"
        )
        invocation = LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir, model=model)
        execute = getattr(self.llm_router, "execute", None)
        if execute is not None:
            result, backend = execute(invocation, telemetry=self.telemetry)
        else:
            executor, backend = self.llm_router.resolve()
            result = executor.execute(invocation)
        parsed = _parse_red_team_llm_response(result.response_text)
        return {
            "enabled": True,
            "available": True,
            "backend": backend,
            "prompt_path": str(result.prompt_path),
            "response_path": str(result.response_path),
            "diagnostics": result.diagnostics,
            "summary": str(parsed.get("summary") or "").strip(),
            "ready_for_human_review": bool(parsed.get("ready_for_human_review", False)),
            "findings": list(parsed.get("findings") or []),
        }

    def _build_mermaid_red_team_prompt(self, *, target_path: Path, review: dict[str, object]) -> str:
        rubric_path = Path(__file__).resolve().parents[2] / "specs" / "mermaid-red-team.md"
        rubric_text = rubric_path.read_text(encoding="utf-8") if rubric_path.exists() else ""
        return (
            "You are red-teaming a Mermaid architecture/control diagram before human review.\n"
            "Use the rubric below. Find structural flaws, intent drift, planning blockage, boundary blur, control ambiguity, ownership inflation, and implementer traps.\n"
            "Prefer concrete findings over generic praise. If there are no major issues, say so explicitly.\n"
            "Return JSON only with keys: summary, ready_for_human_review, findings.\n"
            "findings must be an array of objects with keys: severity, class, summary, rationale, patch_hint.\n\n"
            f"Rubric:\n{rubric_text}\n\n"
            f"Target path: {target_path}\n"
            f"Deterministic review:\n{json.dumps(review, indent=2, sort_keys=True)}\n"
        )


def _extract_mermaid_blocks(text: str) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    fence_pattern = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
    for match in fence_pattern.finditer(text):
        blocks.append(
            {
                "content": match.group(1).strip(),
                "start": match.start(),
                "end": match.end(),
                "kind": "markdown_fence",
            }
        )
    if blocks:
        return blocks
    stripped = text.strip()
    if stripped.startswith(("flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram", "erDiagram", "journey", "mindmap")):
        return [{"content": stripped, "start": 0, "end": len(text), "kind": "mermaid_file"}]
    return []


def _review_mermaid_text(text: str, *, path: Path, block_index: int, diagram_only: bool = False) -> dict[str, object]:
    blocks = [{"content": text.strip(), "start": 0, "end": len(text), "kind": "inline_mermaid"}] if diagram_only else _extract_mermaid_blocks(text)
    if not blocks:
        raise ValueError(f"No Mermaid block found in {path}")
    if block_index < 0 or block_index >= len(blocks):
        raise ValueError(f"Mermaid block index {block_index} is out of range for {path} (found {len(blocks)} blocks)")
    block = blocks[block_index]
    diagram = str(block["content"])
    source_type = str(block["kind"])
    after_text = text[int(block["end"]):] if not diagram_only else ""
    lowered_diagram = diagram.lower()
    lowered_after = after_text.lower()
    control_keywords = ("execution", "gate", "control", "retry", "planner", "contextservice", "context recorder", "packet")
    control_like = any(keyword in lowered_diagram for keyword in control_keywords)
    has_execution_contract = "execution contract" in lowered_after[:4000] or "invariant" in lowered_after[:4000]
    findings: list[dict[str, object]] = []
    checks: list[dict[str, object]] = []

    def add_check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    def add_finding(severity: str, finding_class: str, summary: str, evidence: str, patch_hint: str) -> None:
        findings.append(
            {
                "severity": severity,
                "class": finding_class,
                "summary": summary,
                "evidence": evidence,
                "patch_hint": patch_hint,
            }
        )

    add_check("has_mermaid_block", True, f"Found {len(blocks)} Mermaid block(s); reviewing block {block_index}.")
    execution_contract_ok = diagram_only or (not control_like) or has_execution_contract
    add_check(
        "has_execution_contract",
        execution_contract_ok,
        "Execution contract found near diagram." if has_execution_contract else ("Diagram-only review; contract not required in this pass." if diagram_only else "No nearby execution contract or invariant block found."),
    )
    if control_like and not has_execution_contract and not diagram_only:
        add_finding(
            "major",
            "Implementer Trap",
            "Control Mermaid has no nearby execution contract or invariant block.",
            "The diagram contains control/execution concepts but the following spec text does not lock implementation semantics locally.",
            "Add an `Execution Contract` directly below the diagram with read/write, gating order, partial-information, and additive-only rules.",
        )

    ambiguous_patterns = [
        ("ready?", "ready without specifying ready for what"),
        ("sufficient?", "sufficient without specifying sufficient for what"),
        ("present?", "present without specifying which decision it governs"),
        ("complete?", "complete without specifying complete for what"),
        ("valid?", "valid without specifying what validity means"),
    ]
    ambiguous_hits = [detail for token, detail in ambiguous_patterns if token in lowered_diagram]
    add_check(
        "ambiguous_gate_labels",
        not ambiguous_hits,
        "No generic ambiguous gate labels detected." if not ambiguous_hits else "; ".join(ambiguous_hits),
    )
    for detail in ambiguous_hits:
        add_finding(
            "major",
            "Control Ambiguity",
            "Diagram contains a broad gate label that can be misread.",
            detail,
            "Rewrite the gate label so it names the exact scope, for example `Execution artifacts sufficient for execution?`.",
        )

    read_write_blur = (
        "caller mode" in lowered_diagram
        and ("mutation flow" in lowered_diagram or "contextrecorder" in lowered_diagram or "write caller" in lowered_diagram)
        and ("responder" in lowered_diagram or "ui" in lowered_diagram or "investigation" in lowered_diagram)
        and "read caller mode" not in lowered_diagram
    )
    add_check(
        "read_write_boundary",
        not read_write_blur,
        "Read/write paths appear separated." if not read_write_blur else "Mutation appears blended into a generic caller-mode branch.",
    )
    if read_write_blur:
        add_finding(
            "major",
            "Boundary Blur",
            "Mutation flow appears as a caller mode instead of a companion write boundary.",
            "The diagram mixes read consumers and mutation consumers under the same branch structure.",
            "Separate read callers from write callers and show mutation through a companion recorder boundary.",
        )

    if "build_packet(objective_id" in lowered_diagram and ("project" in text.lower() or "operator scope" in text.lower()):
        add_check("scope_alignment", False, "Entrypoint implies objective-only scope while surrounding spec discusses project/operator scope.")
        add_finding(
            "major",
            "Intent Drift",
            "Entrypoint scope is narrower than the surrounding design.",
            "The diagram entrypoint references `objective_id` only even though nearby spec text includes project/operator scope.",
            "Widen the entrypoint signature or node label so the intended scope is explicit.",
        )
    else:
        add_check("scope_alignment", True, "Entrypoint scope is not obviously narrower than the surrounding spec.")

    ready_for_human_review = not any(item["severity"] in {"critical", "major"} for item in findings)
    return {
        "path": str(path),
        "block_index": block_index,
        "block_count": len(blocks),
        "source_type": source_type,
        "diagram": diagram,
        "checks": checks,
        "findings": findings,
        "ready_for_human_review": ready_for_human_review,
    }


def _parse_red_team_llm_response(text: str) -> dict[str, object]:
    candidates = [text.strip()]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(item.strip() for item in fenced if item.strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            findings = parsed.get("findings")
            return {
                "summary": str(parsed.get("summary") or "").strip(),
                "ready_for_human_review": bool(parsed.get("ready_for_human_review", False)),
                "findings": findings if isinstance(findings, list) else [],
            }
    return {
        "summary": "",
        "ready_for_human_review": False,
        "findings": [
            {
                "severity": "major",
                "class": "Implementer Trap",
                "summary": "LLM review returned an unreadable response.",
                "rationale": "The critique pass could not be parsed as the required JSON payload.",
                "patch_hint": "Tighten the review prompt or rerun with `--no-llm` and inspect the raw response artifact.",
            }
        ],
    }
