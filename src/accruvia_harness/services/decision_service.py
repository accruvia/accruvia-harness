from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain import (
    Decision,
    DecisionAction,
    Event,
    EvaluationVerdict,
    RunStatus,
    TaskStatus,
    new_id,
)
from ..policy import DefaultDecider

if TYPE_CHECKING:
    from ..store import SQLiteHarnessStore

logger = logging.getLogger(__name__)


class DecisionService:
    """Queue-driven decision processor.

    Dequeues pending decision items, runs the decider, persists the
    decision, applies status updates, and emits events.
    """

    def __init__(self, store: SQLiteHarnessStore, decider: DefaultDecider | None = None) -> None:
        self.store = store
        self.decider = decider or DefaultDecider()

    def process_one(self) -> bool:
        """Process the next pending decision queue item.

        Returns True if an item was processed, False if the queue was empty.
        """
        item = self.store.dequeue_decision()
        if item is None:
            return False

        try:
            run = self.store.get_run(item.run_id)
            task = self.store.get_task(item.task_id)
            if run is None or task is None:
                logger.warning(
                    "Decision queue item %s references missing run=%s or task=%s; marking failed",
                    item.id,
                    item.run_id,
                    item.task_id,
                )
                self.store.complete_decision(item.id, "failed")
                return True

            evaluations = self.store.list_evaluations(item.run_id)
            evaluation = next((e for e in evaluations if e.id == item.evaluation_id), None)
            if evaluation is None:
                logger.warning(
                    "Decision queue item %s references missing evaluation=%s; marking failed",
                    item.id,
                    item.evaluation_id,
                )
                self.store.complete_decision(item.id, "failed")
                return True

            # Build AnalyzeResult from stored evaluation.
            from ..policy import AnalyzeResult

            analysis = AnalyzeResult(
                verdict=evaluation.verdict,
                confidence=evaluation.confidence,
                summary=evaluation.summary,
                details=evaluation.details,
            )

            # Run the decider.
            decision_result = self.decider.decide(analysis, run, task)

            # Persist the Decision record.
            decision = Decision(
                id=new_id("decision"),
                run_id=run.id,
                action=decision_result.action,
                rationale=decision_result.rationale,
            )
            self.store.create_decision(decision)

            # Emit decision_recorded event.
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="decision",
                    entity_id=decision.id,
                    event_type="decision_recorded",
                    payload={"run_id": run.id, "action": decision.action.value},
                )
            )

            # Determine final run status.
            final_status = (
                RunStatus.COMPLETED
                if decision_result.action == DecisionAction.PROMOTE
                else RunStatus.FAILED
            )
            if analysis.verdict == EvaluationVerdict.BLOCKED:
                final_status = RunStatus.BLOCKED
            if decision_result.action == DecisionAction.BRANCH:
                final_status = RunStatus.FAILED

            # Determine task status.
            task_status = (
                TaskStatus.COMPLETED
                if decision_result.action == DecisionAction.PROMOTE
                else TaskStatus.PENDING
            )
            if decision_result.action == DecisionAction.FAIL:
                task_status = TaskStatus.FAILED
            if decision_result.action == DecisionAction.BRANCH:
                task_status = TaskStatus.ACTIVE

            # Apply run status.
            self.store.mark_run(run, final_status, decision_result.rationale)

            # Apply task status (guard against reopening completed tasks).
            current_task = self.store.get_task(task.id)
            if current_task is not None and not (
                current_task.status == TaskStatus.COMPLETED and task_status != TaskStatus.COMPLETED
            ):
                self.store.update_task_status(task.id, task_status)
                self.store.create_event(
                    Event(
                        id=new_id("event"),
                        entity_type="task",
                        entity_id=task.id,
                        event_type="task_status_changed",
                        payload={"status": task_status.value, "run_id": run.id},
                    )
                )

            # Mark queue item complete.
            self.store.complete_decision(item.id, "completed")
            return True

        except Exception:
            logger.exception("Failed to process decision queue item %s", item.id)
            self.store.complete_decision(item.id, "failed")
            return True
