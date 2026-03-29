from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.control_breadcrumbs import BreadcrumbWriter
from accruvia_harness.control_classifier import FailureClassifier
from accruvia_harness.control_plane import ControlPlane
from accruvia_harness.control_watch import ControlWatchService
from accruvia_harness.domain import Objective, ObjectiveStatus, Project, new_id
from accruvia_harness.store import SQLiteHarnessStore


class FailureClassifierTests(unittest.TestCase):
    def test_classifies_rate_limit_without_retry(self) -> None:
        result = FailureClassifier().classify("API rate limit reached. Provider returned 429.")

        self.assertEqual("provider_rate_limit", result.classification)
        self.assertFalse(result.retry_recommended)
        self.assertEqual(1800, result.cooldown_seconds)

    def test_classifies_timeout_as_retryable(self) -> None:
        result = FailureClassifier().classify("Worker timed out after 1800 seconds.")

        self.assertEqual("timeout", result.classification)
        self.assertTrue(result.retry_recommended)


class BreadcrumbWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace_root = root / "workspace"
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()

    def test_writes_bundle_and_indexes_it(self) -> None:
        writer = BreadcrumbWriter(self.store, self.workspace_root)

        bundle_dir = writer.write_bundle(
            entity_type="task",
            entity_id="task_123",
            meta={"task_id": "task_123"},
            evidence={"checks": [{"name": "tests", "result": "pass"}]},
            decision={"classification": "timeout", "retry_recommended": True},
            worker_run_id="run_123",
            summary="Tests passed but worker timed out after validation.",
        )

        self.assertTrue((bundle_dir / "meta.json").exists())
        self.assertTrue((bundle_dir / "evidence.json").exists())
        self.assertTrue((bundle_dir / "decision.json").exists())
        self.assertTrue((bundle_dir / "summary.txt").exists())

        indexed = self.store.list_control_breadcrumbs(entity_type="task", entity_id="task_123")
        self.assertEqual(1, len(indexed))
        self.assertEqual("run_123", indexed[0].worker_run_id)
        self.assertEqual("timeout", indexed[0].classification)


class ControlWatchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace_root = root / "workspace"
        self.supervisor_dir = root / "supervisors"
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()
        self.control_plane = ControlPlane(self.store)
        self.control_plane.turn_on()
        self.watch = ControlWatchService(
            self.store,
            self.control_plane,
            FailureClassifier(),
            BreadcrumbWriter(self.store, self.workspace_root),
            supervisor_control_dir=self.supervisor_dir,
        )

    def test_watch_degrades_when_no_supervisor_is_running(self) -> None:
        result = self.watch.run_once()

        self.assertFalse(result["harness"]["ok"])
        self.assertEqual("degraded", result["status"]["global_state"])
        breadcrumbs = self.store.list_control_breadcrumbs(entity_type="lane", entity_id="harness")
        self.assertEqual("system_failure", breadcrumbs[0].classification)

    def test_watch_freezes_on_stalled_objective(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Stalled objective",
            summary="stalled",
            status=ObjectiveStatus.EXECUTING,
        )
        self.store.create_objective(objective)
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE objectives SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (objective.id,),
            )

        result = self.watch.run_once(stalled_objective_hours=1.0)

        self.assertEqual("frozen", result["status"]["global_state"])
        self.assertEqual(objective.id, result["stalled_objectives"][0]["objective_id"])
