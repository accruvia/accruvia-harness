from __future__ import annotations

from dataclasses import dataclass
from time import monotonic as _monotonic
from time import sleep as _sleep
from typing import Callable

from ..domain import Event, new_id


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
        should_stop = stop_requested or (lambda: False)
        progress = progress_callback or (lambda _event: None)

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
                        progress(
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
                            progress(
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
                            progress(
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
                    progress(
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
                        }
                    )
                    recommended = None
                    if hasattr(heartbeat, "analysis") and isinstance(heartbeat.analysis, dict):
                        recommended = heartbeat.analysis.get("next_heartbeat_seconds")
                    if isinstance(recommended, (int, float)) and recommended > 0:
                        heartbeat_intervals[heartbeat_project_id] = float(recommended)
                idle_cycles = 0
                continue

            result = self.queue.process_next_task(
                project_id=project_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                exclude_task_ids=attempted_task_ids,
                progress_callback=progress,
            )
            if result is not None:
                processed_task_ids.append(result["task"].id)
                attempted_task_ids.add(result["task"].id)
                progress(
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

            if review_check_enabled and review_check_interval_seconds is not None and review_watcher is not None:
                review_result = review_watcher.check_due_reviews(review_check_interval_seconds)
                if review_result.checked_count > 0:
                    review_check_count += review_result.checked_count
                    review_conflict_count += review_result.conflict_count
                    review_merged_count += review_result.merged_count
                    progress(
                        {
                            "type": "review_checked",
                            "checked_count": review_result.checked_count,
                            "conflict_count": review_result.conflict_count,
                            "merged_count": review_result.merged_count,
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
            progress(
                {
                    "type": "sleeping",
                    "idle_cycles": idle_cycles,
                    "seconds": idle_sleep_seconds,
                }
            )
            self._sleep(idle_sleep_seconds)
            sleep_count += 1
            slept_seconds += idle_sleep_seconds

        progress(
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
