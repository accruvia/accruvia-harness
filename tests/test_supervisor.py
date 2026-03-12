from __future__ import annotations

import unittest

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


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


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
        self.assertEqual("task_started", events[0]["type"])
        self.assertEqual("task_finished", events[1]["type"])
        self.assertEqual({"pending": 1}, events[1]["backlog_before"]["tasks_by_status"])
        self.assertEqual({"completed": 1}, events[1]["backlog_after"]["tasks_by_status"])
        self.assertTrue(any(event["type"] == "heartbeat_succeeded" for event in events))
        self.assertTrue(any(event["type"] == "sleeping" for event in events))
        self.assertEqual("exiting", events[-1]["type"])
