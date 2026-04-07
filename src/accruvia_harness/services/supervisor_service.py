from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from time import monotonic as _monotonic
from time import sleep as _sleep
from typing import Callable

from ..domain import Event, ObjectiveStatus, new_id


@dataclass(slots=True)
class SupervisorResult:
    processed_count: int
    processed_task_ids: list[str]
    heartbeat_count: int
    heartbeat_project_ids: list[str]
    review_check_count: int
    review_conflict_count: int
    review_merged_count: int
    idle_cycles: int
    sleep_count: int
    slept_seconds: float
    exit_reason: str


class SupervisorService:
    _NOISY_ARTIFACT_KINDS = frozenset({"heartbeat", "phase"})
    _MILESTONE_ARTIFACT_KINDS = frozenset({"plan", "report", "compile-output", "test-output", "stdout", "stderr", "artifact"})

    def __init__(
        self,
        store,
        queue_service,
        cognition_service,
        heartbeat_failure_escalation_threshold: int = 3,
        sleeper: Callable[[float], None] = _sleep,
        monotonic: Callable[[], float] = _monotonic,
    ) -> None:
        if heartbeat_failure_escalation_threshold < 1:
            raise ValueError("heartbeat_failure_escalation_threshold must be at least 1")
        self.store = store
        self.queue = queue_service
        self.cognition = cognition_service
        self.heartbeat_failure_escalation_threshold = heartbeat_failure_escalation_threshold
        self._sleep = sleeper
        self._monotonic = monotonic

    def _metrics_snapshot(self, project_id: str | None) -> dict[str, object]:
        return dict(self.store.metrics_snapshot(project_id))

    def _stalled_objective_count(self, project_id: str | None) -> int:
        stalled_objective_ids: set[str] = set()
        for event in self.store.list_control_events(event_type="objective_stalled", limit=500):
            objective = self.store.get_objective(event.entity_id)
            if objective is None:
                continue
            if project_id is not None and objective.project_id != project_id:
                continue
            if objective.status == ObjectiveStatus.RESOLVED:
                continue
            stalled_objective_ids.add(objective.id)
        return len(stalled_objective_ids)

    def _queue_snapshot(self, project_id: str | None) -> dict[str, int]:
        metrics = self._metrics_snapshot(project_id)
        tasks_by_status = dict(metrics.get("tasks_by_status") or {})
        return {
            "pending": int(tasks_by_status.get("pending", 0) or 0),
            "active": int(tasks_by_status.get("active", 0) or 0),
            "stalled": self._stalled_objective_count(project_id),
        }

    @staticmethod
    def _artifact_kind(path: Path) -> str:
        name = path.name
        if name == "worker.heartbeat.json":
            return "heartbeat"
        if name == "phase.txt":
            return "phase"
        if name == "plan.txt":
            return "plan"
        if name == "report.json":
            return "report"
        if name == "compile_output.txt":
            return "compile-output"
        if name == "test_output.txt":
            return "test-output"
        if name.endswith(".stdout.txt"):
            return "stdout"
        if name.endswith(".stderr.txt"):
            return "stderr"
        return "artifact"

    def _artifact_inventory(self, run_dir: Path) -> dict[str, dict[str, object]]:
        inventory: dict[str, dict[str, object]] = {}
        if not run_dir.exists():
            return inventory
        for child in run_dir.iterdir():
            if not child.is_file():
                continue
            stat = child.stat()
            inventory[child.name] = {
                "name": child.name,
                "path": str(child),
                "kind": self._artifact_kind(child),
                "mtime": stat.st_mtime,
                "age_seconds": max(0.0, time.time() - stat.st_mtime),
            }
        return inventory

    def run(
        self,
        project_id: str | None = None,
        worker_id: str = "supervisor",
        lease_seconds: int = 300,
        watch: bool = False,
        idle_sleep_seconds: float = 30.0,
        max_idle_cycles: int | None = 1,
        max_iterations: int | None = None,
        heartbeat_project_ids: list[str] | None = None,
        heartbeat_interval_seconds: float | None = None,
        heartbeat_all_projects: bool = False,
        review_check_enabled: bool = False,
        review_check_interval_seconds: int | None = None,
        review_watcher=None,
        idle_maintenance_callback: Callable[[str | None, Callable[[dict[str, object]], None]], dict[str, object] | None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> SupervisorResult:
        if max_idle_cycles is not None and max_idle_cycles < 1:
            raise ValueError("max_idle_cycles must be at least 1")
        if max_iterations is not None and max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if idle_sleep_seconds < 0:
            raise ValueError("idle_sleep_seconds must be non-negative")
        if heartbeat_interval_seconds is not None and heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")

        processed_task_ids: list[str] = []
        heartbeat_runs: list[str] = []
        heartbeat_intervals: dict[str, float] = {}
        heartbeat_failures: dict[str, int] = {}
        disabled_heartbeat_projects: set[str] = set()
        review_check_count = 0
        review_conflict_count = 0
        review_merged_count = 0
        idle_cycles = 0
        sleep_count = 0
        slept_seconds = 0.0
        exit_reason = "idle"
        heartbeat_last_run: dict[str, float] = {}
        iterations = 0
        attempted_task_ids: set[str] = set()
        run_progress_state: dict[str, dict[str, object]] = {}
        should_stop = stop_requested or (lambda: False)
        progress = progress_callback or (lambda _event: None)

        def emit(event: dict[str, object]) -> None:
            payload = dict(event)
            snapshot_project_id = payload.get("project_id")
            if not isinstance(snapshot_project_id, str):
                snapshot_project_id = project_id
            queue_snapshot = dict(payload.get("queue_snapshot") or self._queue_snapshot(snapshot_project_id))
            payload["queue_snapshot"] = queue_snapshot
            payload.setdefault("queue_depth", int(queue_snapshot.get("pending", 0)) + int(queue_snapshot.get("active", 0)))
            if payload.get("type") == "worker_status":
                run_id = str(payload.get("run_id") or "").strip()
                elapsed_seconds = payload.get("elapsed_seconds")
                elapsed_value = float(elapsed_seconds) if isinstance(elapsed_seconds, (int, float)) else None
                worker_phase = str(payload.get("worker_phase") or "").strip() or "working"
                latest_artifact_path = str(payload.get("latest_artifact_path") or "").strip()
                state = run_progress_state.setdefault(
                    run_id,
                    {
                        "artifacts": {},
                        "phase": "",
                        "phase_started_elapsed": 0.0,
                        "phase_warning_thresholds": set(),
                        "last_meaningful_change_elapsed": None,
                    },
                )
                if elapsed_value is not None:
                    previous_phase = str(state.get("phase") or "")
                    if worker_phase != previous_phase:
                        state["phase"] = worker_phase
                        state["phase_started_elapsed"] = elapsed_value if previous_phase else 0.0
                        state["phase_warning_thresholds"] = set()
                    phase_started_elapsed = float(state.get("phase_started_elapsed") or 0.0)
                    payload["phase_elapsed_seconds"] = max(0.0, elapsed_value - phase_started_elapsed)
                run_dir = Path(latest_artifact_path).parent if latest_artifact_path else None
                if run_dir is not None:
                    artifacts = self._artifact_inventory(run_dir)
                    previous_artifacts = dict(state.get("artifacts") or {})
                    meaningful_changes: list[dict[str, object]] = []
                    observed_milestones: list[str] = []
                    for name, info in sorted(artifacts.items()):
                        kind = str(info.get("kind") or "")
                        if kind in self._MILESTONE_ARTIFACT_KINDS:
                            observed_milestones.append(name)
                        previous = previous_artifacts.get(name)
                        previous_mtime = float(previous.get("mtime")) if isinstance(previous, dict) and isinstance(previous.get("mtime"), (int, float)) else None
                        current_mtime = float(info.get("mtime")) if isinstance(info.get("mtime"), (int, float)) else None
                        if current_mtime is None:
                            continue
                        if previous_mtime is None:
                            change = "created"
                        elif current_mtime > previous_mtime + 1e-6:
                            change = "updated"
                        else:
                            continue
                        artifact_event = {
                            "type": "artifact_observed",
                            "project_id": snapshot_project_id,
                            "task_id": payload.get("task_id"),
                            "task_title": payload.get("task_title"),
                            "run_id": run_id,
                            "worker_phase": worker_phase,
                            "artifact": info.get("name"),
                            "artifact_kind": kind,
                            "artifact_path": info.get("path"),
                            "change": change,
                            "age_seconds": info.get("age_seconds"),
                        }
                        if kind not in self._NOISY_ARTIFACT_KINDS:
                            meaningful_changes.append(artifact_event)
                        elif change == "created":
                            progress({**artifact_event, "queue_snapshot": queue_snapshot, "queue_depth": payload["queue_depth"]})
                    state["artifacts"] = artifacts
                    if meaningful_changes and elapsed_value is not None:
                        state["last_meaningful_change_elapsed"] = elapsed_value
                    last_meaningful_change_elapsed = state.get("last_meaningful_change_elapsed")
                    if isinstance(last_meaningful_change_elapsed, (int, float)) and elapsed_value is not None:
                        payload["meaningful_artifact_age_seconds"] = max(0.0, elapsed_value - float(last_meaningful_change_elapsed))
                    elif elapsed_value is not None:
                        payload["meaningful_artifact_age_seconds"] = elapsed_value
                    payload["milestone_artifacts"] = observed_milestones
                    payload["milestone_artifact_count"] = len(observed_milestones)
                    for artifact_event in meaningful_changes:
                        progress({**artifact_event, "queue_snapshot": queue_snapshot, "queue_depth": payload["queue_depth"]})
                if elapsed_value is not None:
                    phase_elapsed = payload.get("phase_elapsed_seconds")
                    meaningful_age = payload.get("meaningful_artifact_age_seconds")
                    if isinstance(phase_elapsed, (int, float)):
                        emitted = set(state.get("phase_warning_thresholds") or set())
                        for threshold in (600.0, 1200.0):
                            meaningful_age_value = float(meaningful_age) if isinstance(meaningful_age, (int, float)) else float(phase_elapsed)
                            if phase_elapsed >= threshold and meaningful_age_value >= threshold and threshold not in emitted:
                                progress(
                                    {
                                        "type": "phase_dwell_warning",
                                        "project_id": snapshot_project_id,
                                        "task_id": payload.get("task_id"),
                                        "task_title": payload.get("task_title"),
                                        "run_id": run_id,
                                        "worker_phase": worker_phase,
                                        "phase_elapsed_seconds": phase_elapsed,
                                        "meaningful_artifact_age_seconds": payload.get("meaningful_artifact_age_seconds"),
                                        "milestone_artifact_count": payload.get("milestone_artifact_count"),
                                        "queue_snapshot": queue_snapshot,
                                        "queue_depth": payload["queue_depth"],
                                    }
                                )
                                emitted.add(threshold)
                        state["phase_warning_thresholds"] = emitted
            progress(payload)

        while True:
            if should_stop():
                exit_reason = "graceful_stop_requested"
                break
            if max_iterations is not None and iterations >= max_iterations:
                exit_reason = "max_iterations_reached"
                break
            iterations += 1

            due_heartbeats = self._due_heartbeat_projects(
                explicit_project_ids=heartbeat_project_ids or [],
                heartbeat_all_projects=heartbeat_all_projects,
                disabled_project_ids=disabled_heartbeat_projects,
                interval_seconds=heartbeat_interval_seconds,
                last_run=heartbeat_last_run,
                interval_overrides=heartbeat_intervals,
            )
            if due_heartbeats:
                for heartbeat_project_id in due_heartbeats:
                    backlog_before = self._metrics_snapshot(heartbeat_project_id)
                    emit({
                        "type": "heartbeat_running",
                        "project_id": heartbeat_project_id,
                    })
                    try:
                        heartbeat = self.cognition.heartbeat(heartbeat_project_id)
                    except Exception as exc:
                        consecutive_failures = heartbeat_failures.get(heartbeat_project_id, 0) + 1
                        heartbeat_failures[heartbeat_project_id] = consecutive_failures
                        heartbeat_last_run[heartbeat_project_id] = self._monotonic()
                        self.store.create_event(
                            Event(
                                id=new_id("event"),
                                entity_type="project",
                                entity_id=heartbeat_project_id,
                                event_type="heartbeat_failed",
                                payload={
                                    "consecutive_failures": consecutive_failures,
                                    "error_type": type(exc).__name__,
                                    "message": str(exc),
                                },
                            )
                        )
                        emit(
                            {
                                "type": "heartbeat_failed",
                                "project_id": heartbeat_project_id,
                                "consecutive_failures": consecutive_failures,
                                "message": str(exc),
                            }
                        )
                        if consecutive_failures >= self.heartbeat_failure_escalation_threshold:
                            self.store.create_event(
                                Event(
                                    id=new_id("event"),
                                    entity_type="project",
                                    entity_id=heartbeat_project_id,
                                    event_type="heartbeat_escalated",
                                    payload={
                                        "consecutive_failures": consecutive_failures,
                                        "message": str(exc),
                                        "operator_action": "inspect_heartbeat_provider_and_nudge_project",
                                        "threshold": self.heartbeat_failure_escalation_threshold,
                                    },
                                )
                            )
                            emit(
                                {
                                    "type": "heartbeat_escalated",
                                    "project_id": heartbeat_project_id,
                                    "consecutive_failures": consecutive_failures,
                                    "threshold": self.heartbeat_failure_escalation_threshold,
                                }
                            )
                            disabled_heartbeat_projects.add(heartbeat_project_id)
                            self.store.create_event(
                                Event(
                                    id=new_id("event"),
                                    entity_type="project",
                                    entity_id=heartbeat_project_id,
                                    event_type="heartbeat_disabled",
                                    payload={
                                        "consecutive_failures": consecutive_failures,
                                        "reason": "disabled_after_repeated_failures",
                                        "threshold": self.heartbeat_failure_escalation_threshold,
                                    },
                                )
                            )
                            emit(
                                {
                                    "type": "heartbeat_disabled",
                                    "project_id": heartbeat_project_id,
                                    "consecutive_failures": consecutive_failures,
                                    "threshold": self.heartbeat_failure_escalation_threshold,
                                }
                            )
                        continue
                    heartbeat_runs.append(heartbeat_project_id)
                    heartbeat_failures.pop(heartbeat_project_id, None)
                    heartbeat_last_run[heartbeat_project_id] = self._monotonic()
                    backlog_after = self._metrics_snapshot(heartbeat_project_id)
                    recommended = None
                    if hasattr(heartbeat, "analysis") and isinstance(heartbeat.analysis, dict):
                        recommended = heartbeat.analysis.get("next_heartbeat_seconds")
                    interval_seconds = float(heartbeat_interval_seconds or 1800.0)
                    source = "default"
                    if isinstance(recommended, (int, float)) and recommended > 0:
                        interval_seconds = float(recommended)
                        source = "brain_recommended"
                    heartbeat_intervals[heartbeat_project_id] = interval_seconds
                    self.store.create_event(
                        Event(
                            id=new_id("event"),
                            entity_type="project",
                            entity_id=heartbeat_project_id,
                            event_type="heartbeat_scheduled",
                            payload={
                                "interval_seconds": interval_seconds,
                                "source": source,
                            },
                        )
                    )
                    emit(
                        {
                            "type": "heartbeat_succeeded",
                            "project_id": heartbeat_project_id,
                            "heartbeat_count": len(heartbeat_runs),
                            "summary": str(heartbeat.analysis.get("summary") or ""),
                            "created_task_count": len(list(heartbeat.created_tasks or [])),
                            "skipped_task_count": len(list(heartbeat.skipped_tasks or [])),
                            "issue_creation_needed": bool(heartbeat.analysis.get("issue_creation_needed", False)),
                            "backlog_before": backlog_before,
                            "backlog_after": backlog_after,
                            "heartbeat_interval_seconds": interval_seconds,
                            "heartbeat_schedule_source": source,
                        }
                    )
                idle_cycles = 0
                continue

            # Periodically recover stale leases even when busy, so active
            # tasks with expired leases don't accumulate behind the queue.
            if len(processed_task_ids) % 5 == 0 and processed_task_ids:
                recovered = self.store.recover_stale_state()
                if any(int(count or 0) > 0 for count in recovered.values()):
                    emit({"type": "stale_state_recovered", "recovered": recovered})

            result = self.queue.process_next_task(
                project_id=project_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                exclude_task_ids=attempted_task_ids,
                progress_callback=emit,
            )
            # Gate blocked: sleep for the backoff duration, don't count as idle.
            if isinstance(result, dict) and result.get("gate_blocked"):
                sleep_for = min(float(result.get("retry_in_seconds", 30)), 60)
                emit({"type": "gate_backoff", "seconds": sleep_for})
                self._sleep(sleep_for)
                slept_seconds += sleep_for
                sleep_count += 1
                continue
            if result is not None:
                processed_task_ids.append(result["task"].id)
                attempted_task_ids.add(result["task"].id)
                emit(
                    {
                        "type": "task_processed",
                        "task_id": result["task"].id,
                        "task_title": result["task"].title,
                        "status": result["task"].status.value,
                        "run_summary": result["runs"][-1].summary if result.get("runs") else "",
                        "processed_count": len(processed_task_ids),
                    }
                )
                idle_cycles = 0
                continue

            queue_metrics = self._metrics_snapshot(project_id)
            queue_depth = int(queue_metrics.get("tasks_by_status", {}).get("pending", 0)) + int(
                queue_metrics.get("tasks_by_status", {}).get("active", 0)
            )
            if watch and attempted_task_ids and queue_depth > 0:
                attempted_task_ids.clear()
                emit(
                    {
                        "type": "queue_retry_cycle_reset",
                        "queue_depth": queue_depth,
                    }
                )
                continue

            if review_check_enabled and review_check_interval_seconds is not None and review_watcher is not None:
                review_result = review_watcher.check_due_reviews(review_check_interval_seconds)
                if review_result.checked_count > 0:
                    review_check_count += review_result.checked_count
                    review_conflict_count += review_result.conflict_count
                    review_merged_count += review_result.merged_count
                    emit(
                        {
                            "type": "review_checked",
                            "checked_count": review_result.checked_count,
                            "conflict_count": review_result.conflict_count,
                            "merged_count": review_result.merged_count,
                        }
                    )
                    idle_cycles = 0
                    continue

            if idle_maintenance_callback is not None:
                maintenance_result = idle_maintenance_callback(project_id, emit) or {}
                if bool(maintenance_result.get("changed")):
                    idle_cycles = 0
                    continue

            recovered = self.store.recover_stale_state()
            if any(int(count or 0) > 0 for count in recovered.values()):
                emit(
                    {
                        "type": "stale_state_recovered",
                        "recovered": recovered,
                    }
                )
                idle_cycles = 0
                continue

            idle_cycles += 1
            if not watch:
                exit_reason = "idle"
                break
            if should_stop():
                exit_reason = "graceful_stop_requested"
                break
            if max_idle_cycles is not None and idle_cycles >= max_idle_cycles:
                exit_reason = "max_idle_cycles_reached"
                break
            emit(
                {
                    "type": "sleeping",
                    "idle_cycles": idle_cycles,
                    "seconds": idle_sleep_seconds,
                    "queue_depth": queue_depth,
                    "heartbeat_project_ids": list(heartbeat_project_ids or []),
                    "next_heartbeat_seconds": self._next_heartbeat_due_seconds(
                        heartbeat_project_ids or ([] if not heartbeat_all_projects else [p.id for p in self.store.list_projects()]),
                        heartbeat_last_run,
                        heartbeat_intervals,
                    ),
                }
            )
            self._sleep(idle_sleep_seconds)
            sleep_count += 1
            slept_seconds += idle_sleep_seconds

        emit(
            {
                "type": "exiting",
                "exit_reason": exit_reason,
                "processed_count": len(processed_task_ids),
                "heartbeat_count": len(heartbeat_runs),
                "review_check_count": review_check_count,
            }
        )
        return SupervisorResult(
            processed_count=len(processed_task_ids),
            processed_task_ids=processed_task_ids,
            heartbeat_count=len(heartbeat_runs),
            heartbeat_project_ids=heartbeat_runs,
            review_check_count=review_check_count,
            review_conflict_count=review_conflict_count,
            review_merged_count=review_merged_count,
            idle_cycles=idle_cycles,
            sleep_count=sleep_count,
            slept_seconds=slept_seconds,
            exit_reason=exit_reason,
        )

    def _next_heartbeat_due_seconds(
        self,
        project_ids: list[str],
        last_run: dict[str, float],
        intervals: dict[str, float],
    ) -> float | None:
        if not project_ids:
            return None
        now = self._monotonic()
        due_values: list[float] = []
        for project_id in project_ids:
            if project_id not in last_run:
                return 0.0
            interval = float(intervals.get(project_id, 1800.0))
            due_values.append(max(0.0, interval - (now - last_run[project_id])))
        if not due_values:
            return None
        return min(due_values)

    def _due_heartbeat_projects(
        self,
        explicit_project_ids: list[str],
        heartbeat_all_projects: bool,
        disabled_project_ids: set[str],
        interval_seconds: float | None,
        last_run: dict[str, float],
        interval_overrides: dict[str, float] | None = None,
    ) -> list[str]:
        if interval_seconds is None and not interval_overrides:
            return []
        project_ids = explicit_project_ids
        if heartbeat_all_projects:
            project_ids = [project.id for project in self.store.list_projects()]
        if not project_ids:
            return []
        now = self._monotonic()
        return [
            project_id
            for project_id in project_ids
            if project_id not in disabled_project_ids
            if (
                (interval_overrides or {}).get(project_id, interval_seconds) is not None
                and now - last_run.get(project_id, float("-inf"))
                >= (interval_overrides or {}).get(project_id, interval_seconds)
            )
        ]
