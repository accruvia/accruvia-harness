"""Background thread coordinators for the harness UI layer.

Extracted from ui.py to reduce monolith size. These coordinate
concurrent async operations (atomic generation, objective review,
project supervision) via daemon threads with thread-safe guards.
"""
from __future__ import annotations

import datetime as _dt
import threading


class AtomicGenerationCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def start(self, objective_id: str, worker) -> bool:
        with self._lock:
            if objective_id in self._running:
                return False
            self._running.add(objective_id)
        thread = threading.Thread(target=self._run, args=(objective_id, worker), daemon=True)
        thread.start()
        return True

    def _run(self, objective_id: str, worker) -> None:
        try:
            worker()
        finally:
            with self._lock:
                self._running.discard(objective_id)


class ObjectiveReviewCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def start(self, objective_id: str, worker) -> bool:
        with self._lock:
            if objective_id in self._running:
                return False
            self._running.add(objective_id)
        thread = threading.Thread(target=self._run, args=(objective_id, worker), daemon=True)
        thread.start()
        return True

    def _run(self, objective_id: str, worker) -> None:
        try:
            worker()
        finally:
            with self._lock:
                self._running.discard(objective_id)


class BackgroundSupervisorCoordinator:
    """Manages background supervisor threads, one per project."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, threading.Event] = {}
        self._status: dict[str, dict[str, object]] = {}

    def start(self, project_id: str, engine, *, watch: bool = True) -> bool:
        with self._lock:
            if project_id in self._running:
                return False
            stop_event = threading.Event()
            self._running[project_id] = stop_event
            self._status[project_id] = {
                "state": "starting",
                "processed_count": 0,
                "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }

        def worker() -> None:
            try:
                if hasattr(engine.worker, "set_stop_requested"):
                    engine.worker.set_stop_requested(stop_event.is_set)
                self._status[project_id]["state"] = "running"
                result = engine.supervise(
                    project_id=project_id,
                    worker_id=f"ui-supervisor-{project_id[:8]}",
                    watch=watch,
                    idle_sleep_seconds=10.0,
                    max_idle_cycles=None,
                    stop_requested=stop_event.is_set,
                    progress_callback=lambda ev: self._on_progress(project_id, ev),
                )
                self._status[project_id].update({
                    "state": "finished",
                    "processed_count": result.processed_count,
                    "exit_reason": result.exit_reason,
                    "finished_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                })
            except Exception as exc:
                self._status[project_id].update({
                    "state": "error",
                    "error": str(exc),
                    "finished_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                })
            finally:
                if hasattr(engine.worker, "set_stop_requested"):
                    engine.worker.set_stop_requested(None)
                with self._lock:
                    self._running.pop(project_id, None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return True

    def stop(self, project_id: str) -> bool:
        with self._lock:
            stop_event = self._running.get(project_id)
            if stop_event is None:
                return False
            stop_event.set()
            status = self._status.get(project_id, {})
            status["state"] = "stopping"
            return True

    def is_running(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._running

    def status(self, project_id: str) -> dict[str, object]:
        return dict(self._status.get(project_id, {"state": "idle"}))

    def _on_progress(self, project_id: str, event: dict[str, object]) -> None:
        event_type = event.get("type", "")
        status = self._status.get(project_id, {})
        if event_type == "task_finished":
            status["processed_count"] = status.get("processed_count", 0) + 1
            status["last_task_id"] = event.get("task_id")
            status["last_task_title"] = event.get("task_title")
            status["last_task_status"] = event.get("status")
        status["last_event"] = event_type
        status["last_event_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
