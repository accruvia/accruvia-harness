from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import (
    Decision,
    DecisionAction,
    DecisionQueueItem,
    Evaluation,
    EvaluationVerdict,
    Event,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    new_id,
)
from accruvia_harness.services.decision_service import DecisionService
from accruvia_harness.store import SQLiteHarnessStore


def _make_store(tmp: str) -> SQLiteHarnessStore:
    store = SQLiteHarnessStore(Path(tmp) / "harness.db")
    store.initialize()
    return store


def _seed(store: SQLiteHarnessStore) -> tuple[Task, Run, Evaluation]:
    """Create a minimal project/task/run/evaluation for decision tests."""
    from accruvia_harness.domain import Project

    project = Project(id=new_id("project"), name="test-proj", description="Test project")
    store.create_project(project)
    task = Task(
        id=new_id("task"),
        project_id=project.id,
        title="Test task",
        objective="Do the thing",
        max_attempts=3,
    )
    store.create_task(task)
    run = Run(
        id=new_id("run"),
        task_id=task.id,
        status=RunStatus.DECIDING,
        attempt=1,
        summary="",
    )
    store.create_run(run)
    evaluation = Evaluation(
        id=new_id("eval"),
        run_id=run.id,
        verdict=EvaluationVerdict.ACCEPTABLE,
        confidence=0.95,
        summary="All good",
        details={},
    )
    store.create_evaluation(evaluation)
    return task, run, evaluation


class TestDecisionServiceProcessOne(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_false_when_queue_empty(self) -> None:
        svc = DecisionService(self.store)
        self.assertFalse(svc.process_one())

    def test_promote_sets_completed_statuses(self) -> None:
        task, run, evaluation = _seed(self.store)
        self.store.enqueue_decision(
            DecisionQueueItem(
                id=new_id("dq"),
                run_id=run.id,
                task_id=task.id,
                evaluation_id=evaluation.id,
            )
        )
        svc = DecisionService(self.store)
        result = svc.process_one()

        self.assertTrue(result)
        updated_run = self.store.get_run(run.id)
        self.assertIsNotNone(updated_run)
        self.assertEqual(updated_run.status, RunStatus.COMPLETED)
        updated_task = self.store.get_task(task.id)
        self.assertIsNotNone(updated_task)
        self.assertEqual(updated_task.status, TaskStatus.COMPLETED)

    def test_promote_creates_decision_record(self) -> None:
        task, run, evaluation = _seed(self.store)
        self.store.enqueue_decision(
            DecisionQueueItem(
                id=new_id("dq"),
                run_id=run.id,
                task_id=task.id,
                evaluation_id=evaluation.id,
            )
        )
        svc = DecisionService(self.store)
        svc.process_one()

        decisions = self.store.list_decisions(run.id)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, DecisionAction.PROMOTE)

    def test_promote_emits_decision_recorded_event(self) -> None:
        task, run, evaluation = _seed(self.store)
        self.store.enqueue_decision(
            DecisionQueueItem(
                id=new_id("dq"),
                run_id=run.id,
                task_id=task.id,
                evaluation_id=evaluation.id,
            )
        )
        svc = DecisionService(self.store)
        svc.process_one()

        events = self.store.list_events(entity_type="decision")
        decision_events = [e for e in events if e.event_type == "decision_recorded"]
        self.assertEqual(len(decision_events), 1)
        self.assertEqual(decision_events[0].payload["action"], "promote")

    def test_retry_sets_pending_task_and_failed_run(self) -> None:
        task, run, evaluation = _seed(self.store)
        # Override evaluation to INCOMPLETE so decider returns RETRY
        evaluation_incomplete = Evaluation(
            id=new_id("eval"),
            run_id=run.id,
            verdict=EvaluationVerdict.INCOMPLETE,
            confidence=0.5,
            summary="Missing artifacts",
            details={},
        )
        self.store.create_evaluation(evaluation_incomplete)
        self.store.enqueue_decision(
            DecisionQueueItem(
                id=new_id("dq"),
                run_id=run.id,
                task_id=task.id,
                evaluation_id=evaluation_incomplete.id,
            )
        )
        svc = DecisionService(self.store)
        svc.process_one()

        updated_run = self.store.get_run(run.id)
        self.assertEqual(updated_run.status, RunStatus.FAILED)
        updated_task = self.store.get_task(task.id)
        self.assertEqual(updated_task.status, TaskStatus.PENDING)

    def test_fail_on_exhausted_attempts(self) -> None:
        task, run, evaluation = _seed(self.store)
        # Set attempt = max_attempts with INCOMPLETE verdict -> FAIL
        exhausted_run = Run(
            id=new_id("run"),
            task_id=task.id,
            status=RunStatus.DECIDING,
            attempt=3,
            summary="",
        )
        self.store.create_run(exhausted_run)
        evaluation_fail = Evaluation(
            id=new_id("eval"),
            run_id=exhausted_run.id,
            verdict=EvaluationVerdict.INCOMPLETE,
            confidence=0.3,
            summary="Still broken",
            details={},
        )
        self.store.create_evaluation(evaluation_fail)
        self.store.enqueue_decision(
            DecisionQueueItem(
                id=new_id("dq"),
                run_id=exhausted_run.id,
                task_id=task.id,
                evaluation_id=evaluation_fail.id,
            )
        )
        svc = DecisionService(self.store)
        svc.process_one()

        updated_run = self.store.get_run(exhausted_run.id)
        self.assertEqual(updated_run.status, RunStatus.FAILED)
        updated_task = self.store.get_task(task.id)
        self.assertEqual(updated_task.status, TaskStatus.FAILED)

    def test_missing_run_marks_item_failed(self) -> None:
        task, run, evaluation = _seed(self.store)
        self.store.enqueue_decision(
            DecisionQueueItem(
                id=new_id("dq"),
                run_id="run_nonexistent",
                task_id=task.id,
                evaluation_id=evaluation.id,
            )
        )
        svc = DecisionService(self.store)
        result = svc.process_one()

        self.assertTrue(result)
        # No decisions should have been created for the missing run
        decisions = self.store.list_decisions("run_nonexistent")
        self.assertEqual(len(decisions), 0)

    def test_missing_evaluation_marks_item_failed(self) -> None:
        task, run, evaluation = _seed(self.store)
        self.store.enqueue_decision(
            DecisionQueueItem(
                id=new_id("dq"),
                run_id=run.id,
                task_id=task.id,
                evaluation_id="eval_nonexistent",
            )
        )
        svc = DecisionService(self.store)
        result = svc.process_one()

        self.assertTrue(result)
        decisions = self.store.list_decisions(run.id)
        self.assertEqual(len(decisions), 0)

    def test_queue_item_marked_completed(self) -> None:
        task, run, evaluation = _seed(self.store)
        item_id = new_id("dq")
        self.store.enqueue_decision(
            DecisionQueueItem(
                id=item_id,
                run_id=run.id,
                task_id=task.id,
                evaluation_id=evaluation.id,
            )
        )
        svc = DecisionService(self.store)
        svc.process_one()

        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT status, completed_at FROM decision_queue WHERE id = ?",
                (item_id,),
            ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["completed_at"])


if __name__ == "__main__":
    unittest.main()
