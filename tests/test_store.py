from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import Event, Project, Task, new_id
from accruvia_harness.store import SQLiteHarnessStore


class SQLiteHarnessStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "harness.db"
        self.store = SQLiteHarnessStore(self.db_path)
        self.store.initialize()

    def test_task_round_trip_preserves_policy_fields(self) -> None:
        project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(project)

        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Runner task",
            objective="Exercise policy persistence",
            priority=250,
            parent_task_id="task_parent",
            source_run_id="run_source",
            external_ref_type="gitlab_issue",
            external_ref_id="456",
            strategy="baseline",
            max_attempts=5,
            required_artifacts=["plan", "report", "diff"],
        )
        self.store.create_task(task)

        loaded = self.store.get_task(task.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(250, loaded.priority)
        self.assertEqual("task_parent", loaded.parent_task_id)
        self.assertEqual("run_source", loaded.source_run_id)
        self.assertEqual("gitlab_issue", loaded.external_ref_type)
        self.assertEqual("456", loaded.external_ref_id)
        self.assertEqual("baseline", loaded.strategy)
        self.assertEqual(5, loaded.max_attempts)
        self.assertEqual(["plan", "report", "diff"], loaded.required_artifacts)

    def test_event_round_trip_preserves_payload(self) -> None:
        event = Event(
            id=new_id("event"),
            entity_type="task",
            entity_id="task_123",
            event_type="task_created",
            payload={"max_attempts": 3, "required_artifacts": ["plan", "report"]},
        )
        self.store.create_event(event)
        loaded = self.store.list_events(entity_type="task", entity_id="task_123")
        self.assertEqual(1, len(loaded))
        self.assertEqual("task_created", loaded[0].event_type)
        self.assertEqual(["plan", "report"], loaded[0].payload["required_artifacts"])

    def test_task_leases_are_acquired_and_released(self) -> None:
        project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Lease me",
            objective="Test worker leasing",
        )
        self.store.create_task(task)

        leased = self.store.acquire_task_lease("worker-a", lease_seconds=60)

        self.assertIsNotNone(leased)
        leases = self.store.list_task_leases()
        self.assertEqual(1, len(leases))
        self.assertEqual(task.id, leases[0].task_id)
        self.assertEqual("worker-a", leases[0].worker_id)

        self.store.release_task_lease(task.id, "worker-a")
        self.assertEqual([], self.store.list_task_leases())

    def test_active_lease_blocks_second_worker_until_release(self) -> None:
        project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Lease me once",
            objective="Prevent duplicate acquisition",
        )
        self.store.create_task(task)

        first = self.store.acquire_task_lease("worker-a", lease_seconds=60)
        second = self.store.acquire_task_lease("worker-b", lease_seconds=60)

        self.assertEqual(task.id, first.id if first else None)
        self.assertIsNone(second)

        self.store.release_task_lease(task.id, "worker-a")
        third = self.store.acquire_task_lease("worker-b", lease_seconds=60)
        self.assertEqual(task.id, third.id if third else None)
