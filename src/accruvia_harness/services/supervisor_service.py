from __future__ import annotations

from dataclasses import dataclass
from time import monotonic as _monotonic
from time import sleep as _sleep
from typing import Callable


@dataclass(slots=True)
class SupervisorResult:
    processed_count: int
    processed_task_ids: list[str]
    heartbeat_count: int
    heartbeat_project_ids: list[str]
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
        sleeper: Callable[[float], None] = _sleep,
        monotonic: Callable[[], float] = _monotonic,
    ) -> None:
        self.store = store
        self.queue = queue_service
        self.cognition = cognition_service
        self._sleep = sleeper
        self._monotonic = monotonic

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
        idle_cycles = 0
        sleep_count = 0
        slept_seconds = 0.0
        exit_reason = "idle"
        heartbeat_last_run: dict[str, float] = {}
        iterations = 0

        while True:
            if max_iterations is not None and iterations >= max_iterations:
                exit_reason = "max_iterations_reached"
                break
            iterations += 1

            result = self.queue.process_next_task(
                project_id=project_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
            if result is not None:
                processed_task_ids.append(result["task"].id)
                idle_cycles = 0
                continue

            due_heartbeats = self._due_heartbeat_projects(
                explicit_project_ids=heartbeat_project_ids or [],
                heartbeat_all_projects=heartbeat_all_projects,
                interval_seconds=heartbeat_interval_seconds,
                last_run=heartbeat_last_run,
            )
            if due_heartbeats:
                for heartbeat_project_id in due_heartbeats:
                    self.cognition.heartbeat(heartbeat_project_id)
                    heartbeat_runs.append(heartbeat_project_id)
                    heartbeat_last_run[heartbeat_project_id] = self._monotonic()
                idle_cycles = 0
                continue

            idle_cycles += 1
            if not watch:
                exit_reason = "idle"
                break
            if max_idle_cycles is not None and idle_cycles >= max_idle_cycles:
                exit_reason = "max_idle_cycles_reached"
                break
            self._sleep(idle_sleep_seconds)
            sleep_count += 1
            slept_seconds += idle_sleep_seconds

        return SupervisorResult(
            processed_count=len(processed_task_ids),
            processed_task_ids=processed_task_ids,
            heartbeat_count=len(heartbeat_runs),
            heartbeat_project_ids=heartbeat_runs,
            idle_cycles=idle_cycles,
            sleep_count=sleep_count,
            slept_seconds=slept_seconds,
            exit_reason=exit_reason,
        )

    def _due_heartbeat_projects(
        self,
        explicit_project_ids: list[str],
        heartbeat_all_projects: bool,
        interval_seconds: float | None,
        last_run: dict[str, float],
    ) -> list[str]:
        if interval_seconds is None:
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
            if now - last_run.get(project_id, float("-inf")) >= interval_seconds
        ]
