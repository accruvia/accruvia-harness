from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.services.supervisor_service import SupervisorService


class _FakeTask:
    def __init__(self, task_id: str) -> None:
        self.id = task_id
        self.title = task_id
        self.status = type("Status", (), {"value": "completed"})()


class _FakeQueue:
    def __init__(self, task_ids: list[str]) -> None:
        self.task_ids = list(task_ids)
        self.calls: list[tuple[str | None, str, int, tuple[str, ...]]] = []

    def process_next_task(
        self,
        project_id=None,
        worker_id="supervisor",
        lease_seconds=300,
        exclude_task_ids=None,
        progress_callback=None,
    ):
        self.calls.append((project_id, worker_id, lease_seconds, tuple(sorted(exclude_task_ids or set()))))
        if not self.task_ids:
            return None
        task_id = self.task_ids.pop(0)
        if progress_callback is not None:
            progress_callback(
                {
                    "type": "task_started",
                    "task_id": task_id,
                    "task_title": task_id,
                    "project_id": project_id or "project",
                }
            )
            progress_callback(
                {
                    "type": "task_finished",
                    "task_id": task_id,
                    "task_title": task_id,
                    "project_id": project_id or "project",
                    "status": "completed",
                    "run_id": f"run-for-{task_id}",
                    "run_status": "completed",
                    "summary": "completed",
                    "backlog_before": {"tasks_by_status": {"pending": 1}, "pending_promotions": 0},
                    "backlog_after": {"tasks_by_status": {"completed": 1}, "pending_promotions": 0},
                }
            )
        return {"task": _FakeTask(task_id), "runs": []}


class _FakeCognition:
    def __init__(self, next_heartbeat_seconds: int | None = None) -> None:
        self.calls: list[str] = []
        self.next_heartbeat_seconds = next_heartbeat_seconds

    def heartbeat(self, project_id: str):
        self.calls.append(project_id)
        return type(
            "HeartbeatStub",
            (),
            {
                "analysis": {
                    "next_heartbeat_seconds": self.next_heartbeat_seconds,
                    "summary": "heartbeat ok",
                    "issue_creation_needed": False,
                },
                "created_tasks": [],
                "skipped_tasks": [],
            },
        )()


class _FakeProject:
    def __init__(self, project_id: str) -> None:
        self.id = project_id


class _FakeStore:
    def __init__(self, project_ids: list[str] | None = None) -> None:
        self.project_ids = project_ids or []
        self.events = []
        self.metrics_calls: dict[str | None, int] = {}
        self.recoveries: list[dict[str, int]] = []

    def list_projects(self):
        return [_FakeProject(project_id) for project_id in self.project_ids]

    def create_event(self, event) -> None:
        self.events.append(event)

    def metrics_snapshot(self, project_id: str | None = None):
        count = self.metrics_calls.get(project_id, 0)
        self.metrics_calls[project_id] = count + 1
        if count == 0:
            return {"tasks_by_status": {"pending": 1}, "pending_promotions": 0}
        return {"tasks_by_status": {"completed": 1}, "pending_promotions": 0}

    def recover_stale_state(self):
        if self.recoveries:
            return self.recoveries.pop(0)
        return {"runs": 0, "tasks": 0, "leases": 0}

    def list_control_events(self, *, event_type: str | None = None, entity_type: str | None = None, entity_id: str | None = None, limit: int | None = None):
        return []

    def get_objective(self, objective_id: str):
        return None


class _FakeReviewWatcher:
    def __init__(self, counts: list[int]) -> None:
        self.counts = list(counts)
        self.calls: list[int] = []

    def check_due_reviews(self, interval_seconds: int):
        self.calls.append(interval_seconds)
        count = self.counts.pop(0) if self.counts else 0
        from accruvia_harness.services.review_watcher_service import ReviewWatcherResult

        return ReviewWatcherResult(
            checked_count=count,
            changed_count=count,
            conflict_count=count,
            merged_count=0,
            checked_promotion_ids=["promotion-1"] if count else [],
        )


class _RetryingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str, int, tuple[str, ...]]] = []
        self.attempts = 0

    def process_next_task(
        self,
        project_id=None,
        worker_id="supervisor",
        lease_seconds=300,
        exclude_task_ids=None,
        progress_callback=None,
    ):
        excluded = tuple(sorted(exclude_task_ids or set()))
        self.calls.append((project_id, worker_id, lease_seconds, excluded))
        if "task-retry" in excluded:
            return None
        self.attempts += 1
        if progress_callback is not None:
            progress_callback(
                {
                    "type": "task_started",
                    "task_id": "task-retry",
                    "task_title": "task-retry",
                    "project_id": project_id or "project",
                }
            )
            progress_callback(
                {
                    "type": "task_finished",
                    "task_id": "task-retry",
                    "task_title": "task-retry",
                    "project_id": project_id or "project",
                    "status": "pending",
                    "run_id": f"run-for-task-retry-{self.attempts}",
                    "run_status": "failed",
                    "summary": "retry pending",
                    "backlog_before": {"tasks_by_status": {"pending": 1}, "pending_promotions": 0},
                    "backlog_after": {"tasks_by_status": {"pending": 1}, "pending_promotions": 0},
                }
            )
        return {"task": _FakeTask("task-retry"), "runs": []}


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _ArtifactQueue:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.calls = 0

    def process_next_task(
        self,
        project_id=None,
        worker_id="supervisor",
        lease_seconds=300,
        exclude_task_ids=None,
        progress_callback=None,
    ):
        self.calls += 1
        if self.calls == 1 and progress_callback is not None:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / "worker.heartbeat.json").write_text("{}", encoding="utf-8")
            (self.run_dir / "phase.txt").write_text("llm_generation\n", encoding="utf-8")
            (self.run_dir / "plan.txt").write_text("plan\n", encoding="utf-8")
            progress_callback(
                {
                    "type": "worker_status",
                    "project_id": project_id or "project",
                    "task_id": "task-artifact",
                    "task_title": "task-artifact",
                    "run_id": "run-artifact",
                    "backend_name": "agent",
                    "pid": 42,
                    "elapsed_seconds": 605,
                    "latest_artifact": "worker.heartbeat.json",
                    "latest_artifact_path": str(self.run_dir / "worker.heartbeat.json"),
                    "latest_artifact_kind": "heartbeat",
                    "latest_artifact_age_seconds": 0.0,
                    "worker_phase": "llm_generation",
                    "command_summary": "bin/accruvia-codex-worker",
                    "stale": False,
                }
            )
        return None


class _DwellQueue:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.calls = 0

    def process_next_task(
        self,
        project_id=None,
        worker_id="supervisor",
        lease_seconds=300,
        exclude_task_ids=None,
        progress_callback=None,
    ):
        self.calls += 1
        if self.calls == 1 and progress_callback is not None:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / "worker.heartbeat.json").write_text("{}", encoding="utf-8")
            (self.run_dir / "phase.txt").write_text("llm_generation\n", encoding="utf-8")
            progress_callback(
                {
                    "type": "worker_status",
                    "project_id": project_id or "project",
                    "task_id": "task-dwell",
                    "task_title": "task-dwell",
                    "run_id": "run-dwell",
                    "backend_name": "agent",
                    "pid": 43,
                    "elapsed_seconds": 605,
                    "latest_artifact": "worker.heartbeat.json",
                    "latest_artifact_path": str(self.run_dir / "worker.heartbeat.json"),
                    "latest_artifact_kind": "heartbeat",
                    "latest_artifact_age_seconds": 0.0,
                    "worker_phase": "llm_generation",
                    "command_summary": "bin/accruvia-codex-worker",
                    "stale": False,
                }
            )
        return None


class _MaintenanceQueue:
    def __init__(self) -> None:
        self.calls = 0

    def process_next_task(
        self,
        project_id=None,
        worker_id="supervisor",
        lease_seconds=300,
        exclude_task_ids=None,
        progress_callback=None,
    ):
        self.calls += 1
        if self.calls == 1:
            return None
        if progress_callback is not None:
            progress_callback(
                {
                    "type": "task_started",
                    "task_id": "task-maintained",
                    "task_title": "task-maintained",
                    "project_id": project_id or "project",
                }
            )
            progress_callback(
                {
                    "type": "task_finished",
                    "task_id": "task-maintained",
                    "task_title": "task-maintained",
                    "project_id": project_id or "project",
                    "status": "completed",
                    "run_id": "run-task-maintained",
                    "run_status": "completed",
                    "summary": "completed",
                    "backlog_before": {"tasks_by_status": {"pending": 1}, "pending_promotions": 0},
                    "backlog_after": {"tasks_by_status": {"completed": 1}, "pending_promotions": 0},
                }
            )
        return {"task": _FakeTask("task-maintained"), "runs": []}


class SupervisorServiceTests(unittest.TestCase):
    def test_supervisor_drains_queue_until_idle(self) -> None:
        queue = _FakeQueue(["task-a", "task-b"])
        cognition = _FakeCognition()
        service = SupervisorService(_FakeStore(), queue, cognition)

        result = service.run(project_id="project-1", worker_id="worker-a", lease_seconds=120)

        self.assertEqual(2, result.processed_count)
        self.assertEqual(["task-a", "task-b"], result.processed_task_ids)
        self.assertEqual(0, result.heartbeat_count)
        self.assertEqual("idle", result.exit_reason)
        self.assertEqual(
            [
                ("project-1", "worker-a", 120, ()),
                ("project-1", "worker-a", 120, ("task-a",)),
                ("project-1", "worker-a", 120, ("task-a", "task-b")),
            ],
            queue.calls,
        )

    def test_supervisor_watch_mode_sleeps_when_idle(self) -> None:
        clock = _FakeClock()
        service = SupervisorService(_FakeStore(), _FakeQueue([]), _FakeCognition(), sleeper=clock.sleep, monotonic=clock.monotonic)

        result = service.run(
            watch=True,
            idle_sleep_seconds=2.5,
            max_idle_cycles=3,
        )

        self.assertEqual(2, result.sleep_count)
        self.assertEqual(5.0, result.slept_seconds)
        self.assertEqual([2.5, 2.5], clock.sleeps)
        self.assertEqual("max_idle_cycles_reached", result.exit_reason)

    def test_supervisor_recovers_stale_state_before_sleeping(self) -> None:
        clock = _FakeClock()
        store = _FakeStore()
        store.recoveries = [
            {"runs": 1, "tasks": 1, "leases": 0},
            {"runs": 0, "tasks": 0, "leases": 0},
            {"runs": 0, "tasks": 0, "leases": 0},
        ]
        progress_events: list[dict[str, object]] = []
        service = SupervisorService(store, _FakeQueue([]), _FakeCognition(), sleeper=clock.sleep, monotonic=clock.monotonic)

        result = service.run(
            watch=True,
            idle_sleep_seconds=2.0,
            max_idle_cycles=2,
            progress_callback=progress_events.append,
        )

        self.assertEqual("max_idle_cycles_reached", result.exit_reason)
        self.assertEqual(1, result.sleep_count)
        self.assertEqual([2.0], clock.sleeps)
        self.assertTrue(any(event["type"] == "stale_state_recovered" for event in progress_events))

    def test_supervisor_runs_heartbeat_only_when_due(self) -> None:
        clock = _FakeClock()
        cognition = _FakeCognition()
        service = SupervisorService(
            _FakeStore(["project-a"]),
            _FakeQueue([]),
            cognition,
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.run(
            watch=True,
            idle_sleep_seconds=5.0,
            max_idle_cycles=3,
            max_iterations=4,
            heartbeat_all_projects=True,
            heartbeat_interval_seconds=10.0,
        )

        self.assertEqual(["project-a", "project-a"], cognition.calls)
        self.assertEqual(2, result.heartbeat_count)
        self.assertEqual(["project-a", "project-a"], result.heartbeat_project_ids)
        self.assertEqual(2, result.sleep_count)
        self.assertEqual("max_iterations_reached", result.exit_reason)

    def test_supervisor_uses_brain_recommended_heartbeat_interval(self) -> None:
        clock = _FakeClock()
        cognition = _FakeCognition(next_heartbeat_seconds=30)
        service = SupervisorService(
            _FakeStore(["project-a"]),
            _FakeQueue([]),
            cognition,
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.run(
            watch=True,
            idle_sleep_seconds=10.0,
            max_idle_cycles=5,
            max_iterations=4,
            heartbeat_all_projects=True,
            heartbeat_interval_seconds=10.0,
        )

        self.assertEqual(["project-a"], cognition.calls)
        self.assertEqual(1, result.heartbeat_count)
        self.assertEqual([10.0, 10.0, 10.0], clock.sleeps)

    def test_supervisor_progress_reports_effective_heartbeat_interval_and_healthy_idle(self) -> None:
        clock = _FakeClock()
        events: list[dict[str, object]] = []
        cognition = _FakeCognition(next_heartbeat_seconds=45)
        service = SupervisorService(
            _FakeStore(["project-a"]),
            _FakeQueue([]),
            cognition,
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )

        service.run(
            project_id="project-a",
            watch=True,
            idle_sleep_seconds=10.0,
            max_idle_cycles=2,
            max_iterations=3,
            heartbeat_all_projects=True,
            heartbeat_interval_seconds=10.0,
            progress_callback=events.append,
        )

        heartbeat = next(event for event in events if event["type"] == "heartbeat_succeeded")
        sleeping = next(event for event in events if event["type"] == "sleeping")
        self.assertEqual(45.0, heartbeat["heartbeat_interval_seconds"])
        self.assertEqual("brain_recommended", heartbeat["heartbeat_schedule_source"])
        self.assertEqual({"pending": 0, "active": 0, "stalled": 0}, heartbeat["queue_snapshot"])
        self.assertEqual(0, sleeping["queue_depth"])
        self.assertEqual({"pending": 0, "active": 0, "stalled": 0}, sleeping["queue_snapshot"])
        self.assertAlmostEqual(45.0, sleeping["next_heartbeat_seconds"])

    def test_supervisor_resets_retry_exclusion_when_pending_work_remains(self) -> None:
        class _RetryMetricsStore(_FakeStore):
            def metrics_snapshot(self, project_id: str | None = None):
                self.metrics_calls[project_id] = self.metrics_calls.get(project_id, 0) + 1
                return {"tasks_by_status": {"pending": 1}, "pending_promotions": 0}

        clock = _FakeClock()
        queue = _RetryingQueue()
        events: list[dict[str, object]] = []
        service = SupervisorService(
            _RetryMetricsStore(),
            queue,
            _FakeCognition(),
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.run(
            project_id="project-a",
            watch=True,
            idle_sleep_seconds=10.0,
            max_idle_cycles=1,
            max_iterations=4,
            progress_callback=events.append,
        )

        self.assertEqual(2, result.processed_count)
        self.assertIn((), [call[3] for call in queue.calls])
        self.assertIn(("task-retry",), [call[3] for call in queue.calls])
        reset = next(event for event in events if event["type"] == "queue_retry_cycle_reset")
        self.assertEqual(1, reset["queue_depth"])

    def test_supervisor_emits_artifact_events_from_worker_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "runs" / "run-artifact"
            events: list[dict[str, object]] = []
            service = SupervisorService(
                _FakeStore(),
                _ArtifactQueue(run_dir),
                _FakeCognition(),
            )

            result = service.run(
                project_id="project-a",
                watch=False,
                max_iterations=1,
                progress_callback=events.append,
            )

        self.assertEqual("idle", result.exit_reason)
        artifact_event = next(event for event in events if event["type"] == "artifact_observed" and event["artifact"] == "plan.txt")
        self.assertEqual("plan", artifact_event["artifact_kind"])
        self.assertEqual("created", artifact_event["change"])
        worker_status = next(event for event in events if event["type"] == "worker_status")
        self.assertEqual(["plan.txt"], worker_status["milestone_artifacts"])
        self.assertEqual(1, worker_status["milestone_artifact_count"])
        self.assertAlmostEqual(0.0, float(worker_status["meaningful_artifact_age_seconds"]))

    def test_supervisor_emits_phase_dwell_warning_when_only_heartbeat_artifacts_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "runs" / "run-dwell"
            events: list[dict[str, object]] = []
            service = SupervisorService(
                _FakeStore(),
                _DwellQueue(run_dir),
                _FakeCognition(),
            )

            service.run(
                project_id="project-a",
                watch=False,
                progress_callback=events.append,
            )

        dwell_warning = next(event for event in events if event["type"] == "phase_dwell_warning")
        self.assertEqual("llm_generation", dwell_warning["worker_phase"])
        self.assertAlmostEqual(605.0, float(dwell_warning["phase_elapsed_seconds"]))

    def test_supervisor_runs_idle_maintenance_before_sleeping(self) -> None:
        clock = _FakeClock()
        events: list[dict[str, object]] = []
        queue = _MaintenanceQueue()
        service = SupervisorService(
            _FakeStore(),
            queue,
            _FakeCognition(),
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )
        maintenance_calls: list[str | None] = []
        maintenance_state = {"first": True}

        def idle_maintenance(project_id, emit):
            maintenance_calls.append(project_id)
            emit(
                {
                    "type": "objective_backlog_resumed",
                    "objective_count": 1,
                    "action_count": 1,
                    "objectives": ["Context Management"],
                }
            )
            changed = maintenance_state["first"]
            maintenance_state["first"] = False
            return {"changed": changed}

        result = service.run(
            project_id="project-a",
            watch=False,
            max_iterations=3,
            idle_maintenance_callback=idle_maintenance,
            progress_callback=events.append,
        )

        self.assertEqual(["project-a"], maintenance_calls)
        self.assertEqual(3, queue.calls)
        self.assertEqual(2, result.processed_count)
        self.assertTrue(any(event["type"] == "objective_backlog_resumed" for event in events))

    def test_supervisor_survives_heartbeat_failure_and_records_event(self) -> None:
        class FailingCognition:
            def heartbeat(self, project_id: str):
                raise RuntimeError("provider unavailable")

        clock = _FakeClock()
        store = _FakeStore(["project-a"])
        service = SupervisorService(
            store,
            _FakeQueue([]),
            FailingCognition(),
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.run(
            watch=True,
            idle_sleep_seconds=10.0,
            max_idle_cycles=2,
            heartbeat_all_projects=True,
            heartbeat_interval_seconds=10.0,
        )

        self.assertEqual("max_idle_cycles_reached", result.exit_reason)
        self.assertEqual(0, result.heartbeat_count)
        self.assertEqual("heartbeat_failed", store.events[0].event_type)
        self.assertEqual(1, store.events[0].payload["consecutive_failures"])

    def test_supervisor_escalates_and_disables_heartbeat_after_threshold(self) -> None:
        class FailingCognition:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def heartbeat(self, project_id: str):
                self.calls.append(project_id)
                raise RuntimeError("provider unavailable")

        clock = _FakeClock()
        store = _FakeStore(["project-a"])
        cognition = FailingCognition()
        service = SupervisorService(
            store,
            _FakeQueue([]),
            cognition,
            heartbeat_failure_escalation_threshold=2,
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.run(
            watch=True,
            idle_sleep_seconds=10.0,
            max_idle_cycles=3,
            max_iterations=6,
            heartbeat_all_projects=True,
            heartbeat_interval_seconds=10.0,
        )

        self.assertEqual("max_idle_cycles_reached", result.exit_reason)
        self.assertEqual(["project-a", "project-a"], cognition.calls)
        self.assertEqual(
            ["heartbeat_failed", "heartbeat_failed", "heartbeat_escalated", "heartbeat_disabled"],
            [event.event_type for event in store.events],
        )
        self.assertEqual(2, store.events[1].payload["consecutive_failures"])
        self.assertEqual(2, store.events[2].payload["threshold"])
        self.assertEqual(2, store.events[3].payload["threshold"])

    def test_supervisor_honors_max_iterations(self) -> None:
        queue = _FakeQueue(["task-a", "task-b", "task-c"])
        service = SupervisorService(_FakeStore(), queue, _FakeCognition())

        result = service.run(max_iterations=2)

        self.assertEqual(2, result.processed_count)
        self.assertEqual("max_iterations_reached", result.exit_reason)

    def test_supervisor_runs_review_checks_only_when_idle(self) -> None:
        clock = _FakeClock()
        watcher = _FakeReviewWatcher([1, 0])
        service = SupervisorService(
            _FakeStore(),
            _FakeQueue([]),
            _FakeCognition(),
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.run(
            watch=True,
            idle_sleep_seconds=5.0,
            max_iterations=3,
            review_check_enabled=True,
            review_check_interval_seconds=28800,
            review_watcher=watcher,
        )

        self.assertEqual([28800, 28800], watcher.calls)
        self.assertEqual(1, result.review_check_count)
        self.assertEqual(1, result.review_conflict_count)

    def test_supervisor_exits_gracefully_when_stop_is_requested(self) -> None:
        queue = _FakeQueue(["task-a"])
        service = SupervisorService(_FakeStore(), queue, _FakeCognition())
        stop_checks = {"count": 0}

        def stop_requested() -> bool:
            stop_checks["count"] += 1
            return stop_checks["count"] >= 3

        result = service.run(
            project_id="project-1",
            worker_id="worker-a",
            lease_seconds=120,
            watch=True,
            idle_sleep_seconds=1.0,
            max_idle_cycles=5,
            stop_requested=stop_requested,
        )

        self.assertEqual(1, result.processed_count)
        self.assertEqual("graceful_stop_requested", result.exit_reason)

    def test_supervisor_emits_progress_events(self) -> None:
        clock = _FakeClock()
        events: list[dict[str, object]] = []
        service = SupervisorService(
            _FakeStore(["project-a"]),
            _FakeQueue(["task-a"]),
            _FakeCognition(),
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.run(
            watch=True,
            idle_sleep_seconds=5.0,
            max_idle_cycles=2,
            max_iterations=4,
            heartbeat_all_projects=True,
            heartbeat_interval_seconds=5.0,
            progress_callback=events.append,
        )

        self.assertEqual("max_iterations_reached", result.exit_reason)
        self.assertEqual("heartbeat_succeeded", events[0]["type"])
        self.assertEqual("task_started", events[1]["type"])
        self.assertEqual("task_finished", events[2]["type"])
        self.assertEqual({"pending": 1}, events[2]["backlog_before"]["tasks_by_status"])
        self.assertEqual({"completed": 1}, events[2]["backlog_after"]["tasks_by_status"])
        self.assertTrue(any(event["type"] == "sleeping" for event in events))
        self.assertEqual("exiting", events[-1]["type"])
