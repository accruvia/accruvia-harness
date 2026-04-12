from __future__ import annotations

from pathlib import Path

from ..domain import (
    DecisionQueueItem,
    Evaluation,
    EvaluationVerdict,
    Event,
    Run,
    RunStatus,
    Task,
    new_id,
)
from ..policy import WorkResult
from ..store import SQLiteHarnessStore


class ValidationService:
    def __init__(self, store: SQLiteHarnessStore) -> None:
        self.store = store

    def validate(
        self, task: Task, run: Run, work: WorkResult, project_workspace_root: Path
    ) -> WorkResult | None:
        return None

    def process_one(
        self,
        task: Task,
        run: Run,
        work: WorkResult,
        project_workspace_root: Path,
    ) -> Run:
        validation_result = self.validate(task, run, work, project_workspace_root)
        if validation_result is not None:
            work = validation_result

        verdict = self._derive_verdict(work)
        evaluation = Evaluation(
            id=new_id("evaluation"),
            run_id=run.id,
            verdict=verdict,
            confidence=1.0 if verdict == EvaluationVerdict.ACCEPTABLE else 0.5,
            summary=work.summary,
            details={"outcome": work.outcome, "diagnostics": work.diagnostics or {}},
        )
        self.store.create_evaluation(evaluation)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="evaluation",
                entity_id=evaluation.id,
                event_type="evaluation_recorded",
                payload={"run_id": run.id, "verdict": evaluation.verdict, "confidence": evaluation.confidence},
            )
        )

        item = DecisionQueueItem(
            id=new_id("dqi"),
            run_id=run.id,
            task_id=task.id,
            evaluation_id=evaluation.id,
        )
        self.store.enqueue_decision(item)

        run = self.store.mark_run(run, RunStatus.DECIDING, "Queued for decision.")

        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="run",
                entity_id=run.id,
                event_type="decision_enqueued",
                payload={
                    "run_id": run.id,
                    "task_id": task.id,
                    "evaluation_id": evaluation.id,
                    "decision_queue_item_id": item.id,
                },
            )
        )

        return run

    @staticmethod
    def _derive_verdict(work: WorkResult) -> EvaluationVerdict:
        if work.outcome == "success":
            return EvaluationVerdict.ACCEPTABLE
        if work.outcome == "blocked":
            return EvaluationVerdict.BLOCKED
        if work.outcome == "failed":
            return EvaluationVerdict.FAILED
        return EvaluationVerdict.INCOMPLETE
