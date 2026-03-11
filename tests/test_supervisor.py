from __future__ import annotations

import unittest

from accruvia_harness.services.supervisor_service import SupervisorService


class _FakeTask:
    def __init__(self, task_id: str) -> None:
        self.id = task_id


class _FakeQueue:
    def __init__(self, task_ids: list[str]) -> None:
        self.task_ids = list(task_ids)
        self.calls: list[tuple[str | None, str, int]] = []

    def process_next_task(self, project_id=None, worker_id="supervisor", lease_seconds=300):
        self.calls.append((project_id, worker_id, lease_seconds))
        if not self.task_ids:
            return None
        task_id = self.task_ids.pop(0)
        return {"task": _FakeTask(task_id), "runs": []}


class _FakeCognition:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def heartbeat(self, project_id: str):
        self.calls.append(project_id)
        return {"project_id": project_id}


class _FakeProject:
    def __init__(self, project_id: str) -> None:
        self.id = project_id


class _FakeStore:
    def __init__(self, project_ids: list[str] | None = None) -> None:
        self.project_ids = project_ids or []

    def list_projects(self):
        return [_FakeProject(project_id) for project_id in self.project_ids]


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
            [("project-1", "worker-a", 120), ("project-1", "worker-a", 120), ("project-1", "worker-a", 120)],
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
