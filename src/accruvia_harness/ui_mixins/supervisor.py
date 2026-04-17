"""HarnessUIDataService supervisor methods."""
from __future__ import annotations

import time
from typing import Any

from ..context_control import objective_execution_gate
from ..domain import Objective, ObjectiveStatus, Task, TaskStatus, serialize_dataclass
from ._shared import _BACKGROUND_SUPERVISOR, _to_jsonable

class SupervisorMixin:

    def start_supervisor(self, project_id: str) -> dict[str, object]:
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        started = _BACKGROUND_SUPERVISOR.start(project_id, self.ctx.engine, watch=True)
        return {
            "started": started,
            "supervisor": _BACKGROUND_SUPERVISOR.status(project_id),
        }


    def stop_supervisor(self, project_id: str) -> dict[str, object]:
        stopped = _BACKGROUND_SUPERVISOR.stop(project_id)
        return {
            "stopped": stopped,
            "supervisor": _BACKGROUND_SUPERVISOR.status(project_id),
        }


    def supervisor_status(self, project_id: str) -> dict[str, object]:
        return {
            "running": _BACKGROUND_SUPERVISOR.is_running(project_id),
            "supervisor": _BACKGROUND_SUPERVISOR.status(project_id),
        }


    def harness_overview(self) -> dict[str, object]:
        with self._harness_overview_cache_lock:
            cached = self._harness_overview_cache
            if cached is not None and (time.monotonic() - cached[0]) < 5.0:
                return cached[1]
        payload = self._build_harness_overview()
        with self._harness_overview_cache_lock:
            self._harness_overview_cache = (time.monotonic(), payload)
        return payload


    def _build_harness_overview(self) -> dict[str, object]:
        """System-wide harness dashboard data."""
        projects = []
        global_counts = {"completed": 0, "active": 0, "failed": 0, "pending": 0}
        active_objectives: list[dict[str, object]] = []
        projects_list = self.store.list_projects()
        tasks_by_project: dict[str, list[Task]] = {}
        for project in projects_list:
            tasks_by_project[project.id] = self.store.list_tasks(project.id)
        for project in projects_list:
            metrics = self.store.metrics_snapshot(project.id)
            tasks_by_status = metrics.get("tasks_by_status", {})
            for status_key in global_counts:
                global_counts[status_key] += int(tasks_by_status.get(status_key, 0))
            objectives = self.store.list_objectives(project.id)
            all_project_tasks = tasks_by_project[project.id]
            active_objective = None
            all_objectives = []
            blocked_pending = 0
            waiting_on_review = 0
            runnable_pending = 0
            for obj in objectives:
                linked_tasks = [t for t in all_project_tasks if t.objective_id == obj.id]
                task_counts = {"completed": 0, "active": 0, "failed": 0, "pending": 0}
                for t in linked_tasks:
                    s = t.status.value if hasattr(t.status, "value") else str(t.status)
                    if s in task_counts:
                        task_counts[s] += 1
                active_task_titles = [t.title for t in linked_tasks if t.status == TaskStatus.ACTIVE]
                needs_workflow = bool(active_task_titles) or task_counts["pending"] > 0 or obj.status in {
                    ObjectiveStatus.EXECUTING,
                    ObjectiveStatus.PLANNING,
                }
                workflow = (
                    self._harness_workflow_status_for_objective(obj, linked_tasks)
                    if needs_workflow
                    else None
                )
                review_ready = bool((workflow or {}).get("review", {}).get("ready"))
                for t in linked_tasks:
                    s = t.status.value if hasattr(t.status, "value") else str(t.status)
                    if s == TaskStatus.PENDING.value:
                        queue_state = self.workflow_service.queue_state_for_task(t, review_ready=review_ready)
                        state = str(queue_state.get("state") or "")
                        if state == "blocked_by_gate":
                            blocked_pending += 1
                        elif state == "waiting_on_review":
                            waiting_on_review += 1
                        elif state == "runnable":
                            runnable_pending += 1
                obj_data = {
                    "id": obj.id,
                    "project_id": project.id,
                    "project_name": project.name,
                    "title": obj.title,
                    "status": obj.status.value,
                    "task_counts": task_counts,
                    "task_total": len(linked_tasks),
                }
                all_objectives.append(obj_data)
                if active_task_titles or task_counts["pending"] > 0 or obj.status in {ObjectiveStatus.EXECUTING, ObjectiveStatus.PLANNING}:
                    active_objectives.append(
                        {
                            **obj_data,
                            "workflow": workflow
                            or {"planning": {"checks": []}, "review": {"checks": []}},
                            "active_task_titles": active_task_titles,
                        }
                    )
                if active_objective is None and obj.status.value in ("executing", "planning"):
                    gen = self._atomic_generation_state(obj.id)
                    active_objective = {**obj_data, "atomic_generation": gen}
            supervisor = _BACKGROUND_SUPERVISOR.status(project.id)
            external_supervisors = self._live_supervisor_records(project.id)
            in_process_running = _BACKGROUND_SUPERVISOR.is_running(project.id)
            running = in_process_running or bool(external_supervisors)
            supervisor_state = supervisor.get("state", "idle")
            if not in_process_running and external_supervisors:
                supervisor_state = "running"
            projects.append({
                "id": project.id,
                "name": project.name,
                "supervisor": {
                    **supervisor,
                    "running": running,
                    "state": supervisor_state,
                    "external_supervisor_count": len(external_supervisors),
                    "external_supervisors": external_supervisors,
                },
                "tasks_by_status": dict(tasks_by_status),
                "pending_queue_states": {
                    "runnable": runnable_pending,
                    "blocked_by_gate": blocked_pending,
                    "waiting_on_review": waiting_on_review,
                },
                "task_total": sum(int(v) for v in tasks_by_status.values()),
                "active_objective": active_objective,
                "objectives": all_objectives,
            })
        # LLM health from router
        llm_health = []
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is not None:
            for name in sorted(llm_router.executors.keys()):
                llm_health.append({
                    "name": name,
                    "demoted": name in llm_router._demoted,
                })
        # Recent events for the feed
        recent_events = []
        for project in projects_list:
            records = self.store.list_context_records(
                project_id=project.id, record_type="action_receipt",
            )
            for record in records[-20:]:
                text = record.content
                if text.startswith("Action receipt: "):
                    text = text[len("Action receipt: "):]
                recent_events.append({
                    "project_id": project.id,
                    "project_name": project.name,
                    "text": text,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id or "",
                    "task_id": record.task_id or "",
                })
            # Also include decomposition telemetry
            telemetry = self.store.list_context_records(
                project_id=project.id, record_type="atomic_decomposition_telemetry",
            )
            for record in telemetry[-10:]:
                recent_events.append({
                    "project_id": project.id,
                    "project_name": project.name,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id or "",
                    "task_id": record.task_id or "",
                })
            # Include completed and failed task events
            all_tasks = tasks_by_project[project.id]
            for t in all_tasks:
                status_val = t.status.value if hasattr(t.status, "value") else str(t.status)
                if status_val == "completed":
                    recent_events.append({
                        "project_id": project.id,
                        "project_name": project.name,
                        "text": f"Task completed: {t.title}",
                        "created_at": t.updated_at.isoformat(),
                        "objective_id": t.objective_id or "",
                        "task_id": t.id,
                        "event_type": "task_completed",
                    })
                elif status_val == "failed":
                    recent_events.append({
                        "project_id": project.id,
                        "project_name": project.name,
                        "text": f"Task failed: {t.title}",
                        "created_at": t.updated_at.isoformat(),
                        "objective_id": t.objective_id or "",
                        "task_id": t.id,
                        "event_type": "task_failed",
                    })
                elif status_val == "active":
                    recent_events.append({
                        "project_id": project.id,
                        "project_name": project.name,
                        "text": f"Task started: {t.title}",
                        "created_at": t.updated_at.isoformat(),
                        "objective_id": t.objective_id or "",
                        "task_id": t.id,
                        "event_type": "task_active",
                    })
        recent_events.sort(key=lambda e: e["created_at"], reverse=True)
        active_objectives.sort(
            key=lambda item: (
                -(int((item.get("task_counts") or {}).get("active", 0))),
                -(int((item.get("task_counts") or {}).get("pending", 0))),
                0 if item.get("status") == ObjectiveStatus.EXECUTING.value else 1,
                str(item.get("project_name") or ""),
                str(item.get("title") or ""),
            )
        )
        return {
            "global_counts": global_counts,
            "global_total": sum(global_counts.values()),
            "active_objectives": active_objectives,
            "projects": projects,
            "llm_health": llm_health,
            "recent_events": recent_events[:50],
        }


    def _harness_workflow_status_for_objective(
        self,
        objective: Objective,
        linked_tasks: list[Task],
    ) -> dict[str, object]:
        planning = self.workflow_service.planning_readiness(objective.id)
        execution = self.workflow_service.execution_readiness(objective.id, linked_tasks)
        review = self.workflow_service.review_readiness(objective.id, linked_tasks)
        current_stage = (
            "review"
            if objective.status == ObjectiveStatus.RESOLVED
            else "execution"
            if objective.status == ObjectiveStatus.EXECUTING
            else "planning"
        )
        return {
            "planning": {
                "stage": planning.stage,
                "ready": planning.ready,
                "checks": _to_jsonable(planning.checks),
            },
            "execution": {
                "stage": execution.stage,
                "ready": execution.ready,
                "checks": _to_jsonable(execution.checks),
            },
            "review": {
                "stage": review.stage,
                "ready": review.ready,
                "checks": _to_jsonable(review.checks),
            },
            "current_stage": current_stage,
        }


    def harness_atomicity_overview(self) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        for project in self.store.list_projects():
            objectives = self.store.list_objectives(project.id)
            tasks = self.store.list_tasks(project.id)
            tasks_by_objective = {
                objective.id: [task for task in tasks if task.objective_id == objective.id]
                for objective in objectives
            }
            for objective in objectives:
                linked_tasks = tasks_by_objective.get(objective.id, [])
                workflow = self._harness_workflow_status_for_objective(objective, linked_tasks)
                gate = objective_execution_gate(self.store, objective.id)
                generation = self._atomic_generation_state(objective.id)
                review = self._promotion_review_for_objective(objective.id, linked_tasks)
                task_counts = {"completed": 0, "active": 0, "failed": 0, "pending": 0}
                latest_activity = objective.updated_at.isoformat() if objective.updated_at else ""
                for task in linked_tasks:
                    status = task.status.value if hasattr(task.status, "value") else str(task.status)
                    if status in task_counts:
                        task_counts[status] += 1
                    if task.updated_at and task.updated_at.isoformat() > latest_activity:
                        latest_activity = task.updated_at.isoformat()
                rows.append(
                    {
                        "id": objective.id,
                        "project_id": project.id,
                        "project_name": project.name,
                        "title": objective.title,
                        "status": objective.status.value,
                        "workflow": workflow,
                        "execution_gate": {
                            "ready": gate.ready,
                            "checks": _to_jsonable(gate.gate_checks),
                        },
                        "atomic_generation": generation,
                        "task_counts": task_counts,
                        "unresolved_failed_count": int(review.get("unresolved_failed_count") or 0),
                        "waived_failed_count": int(review.get("waived_failed_count") or 0),
                        "failed_tasks": list(review.get("failed_tasks") or []),
                        "task_total": sum(task_counts.values()),
                        "latest_activity_at": latest_activity,
                    }
                )
        rows.sort(
            key=lambda item: (
                int((item.get("task_counts") or {}).get("active", 0)),
                int((item.get("task_counts") or {}).get("pending", 0)),
                str(item.get("latest_activity_at") or ""),
                str(item.get("title") or ""),
            ),
            reverse=True,
        )
        return {"objectives": rows}


    def harness_promotion_overview(self) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        for project in self.store.list_projects():
            objectives = self.store.list_objectives(project.id)
            tasks = self.store.list_tasks(project.id)
            tasks_by_objective = {
                objective.id: [task for task in tasks if task.objective_id == objective.id]
                for objective in objectives
            }
            for objective in objectives:
                linked_tasks = tasks_by_objective.get(objective.id, [])
                review = self._promotion_review_for_objective(objective.id, linked_tasks)
                latest_round = (review.get("review_rounds") or [None])[0]
                rows.append(
                    {
                        "id": objective.id,
                        "project_id": project.id,
                        "project_name": project.name,
                        "title": objective.title,
                        "status": objective.status.value,
                        "review_clear": bool(review.get("review_clear")),
                        "next_action": str(review.get("next_action") or ""),
                        "phase": str(review.get("phase") or ""),
                        "review_round_count": len(review.get("review_rounds") or []),
                        "review_packet_count": int(
                            review.get("review_packet_count")
                            or review.get("objective_review_packet_count")
                            or 0
                        ),
                        "unresolved_failed_count": int(review.get("unresolved_failed_count") or 0),
                        "waived_failed_count": int(review.get("waived_failed_count") or 0),
                        "latest_round": latest_round,
                    }
                )
        rows.sort(
            key=lambda item: (
                bool(item.get("review_clear")),
                -int(item.get("unresolved_failed_count") or 0),
                -int(item.get("review_round_count") or 0),
                str(((item.get("latest_round") or {}) if isinstance(item.get("latest_round"), dict) else {}).get("last_activity_at") or ""),
                str(item.get("title") or ""),
            )
        )
        return {"objectives": rows}

