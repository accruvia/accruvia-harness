from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import (
    DecisionQueueItem,
    Event,
    Evaluation,
    Project,
    Run,
    RunStatus,
    Task,
    new_id,
)
from accruvia_harness.policy import WorkResult
from accruvia_harness.services.validation_service import ValidationService
from accruvia_harness.store import SQLiteHarnessStore


class TestValidationServiceProcessOne(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "harness.db"
        self.store = SQLiteHarnessStore(self.db_path)
        self.store.initialize()

        project = Project(
            id=new_id("project"),
            name="test-project",
            description="Test project",
            adapter_name="generic",
        )
        self.store.create_project(project)

        self.task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Test task",
            objective="Validate enqueue flow",
        )
        self.store.create_task(self.task)

        self.run = Run(
            id=new_id("run"),
            task_id=self.task.id,
            status=RunStatus.VALIDATING,
            attempt=1,
            summary="Validating",
        )
        self.store.create_run(self.run)

        self.work = WorkResult(
            summary="All tests passed",
            artifacts=[],
            outcome="success",
        )

        self.workspace = Path(self.temp_dir.name) / "workspace"
        self.workspace.mkdir()

        self.service = ValidationService(self.store)

    def test_process_one_enqueues_decision(self) -> None:
        updated_run = self.service.process_one(self.task, self.run, self.work, self.workspace)

        item = self.store.dequeue_decision()
        assert item is not None
        assert item.run_id == self.run.id
        assert item.task_id == self.task.id
        assert item.status == "processing"

    def test_process_one_records_evaluation(self) -> None:
        self.service.process_one(self.task, self.run, self.work, self.workspace)

        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT id, run_id, verdict FROM evaluations WHERE run_id = ?",
                (self.run.id,),
            ).fetchone()
        assert row is not None
        assert row[1] == self.run.id
        assert row[2] == "acceptable"

    def test_process_one_updates_run_status(self) -> None:
        updated_run = self.service.process_one(self.task, self.run, self.work, self.workspace)

        assert updated_run.status == RunStatus.DECIDING

    def test_process_one_emits_decision_enqueued_event(self) -> None:
        self.service.process_one(self.task, self.run, self.work, self.workspace)

        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT event_type, entity_type, entity_id FROM events WHERE event_type = 'decision_enqueued'",
            ).fetchone()
        assert row is not None
        assert row[1] == "run"
        assert row[2] == self.run.id

    def test_process_one_failed_outcome_verdict(self) -> None:
        failed_work = WorkResult(
            summary="Tests failed",
            artifacts=[],
            outcome="failed",
        )
        self.service.process_one(self.task, self.run, failed_work, self.workspace)

        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT verdict FROM evaluations WHERE run_id = ?",
                (self.run.id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "failed"


if __name__ == "__main__":
    unittest.main()
