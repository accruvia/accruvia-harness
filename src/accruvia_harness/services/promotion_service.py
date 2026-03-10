from __future__ import annotations

from dataclasses import dataclass

from ..domain import Event, PromotionRecord, PromotionStatus, RunStatus, TaskStatus, new_id
from ..store import SQLiteHarnessStore
from ..validation import PromotionValidator, ValidationIssue, default_promotion_validators
from .task_service import TaskService


@dataclass(slots=True)
class PromotionReviewResult:
    promotion: PromotionRecord
    follow_on_task_id: str | None


class PromotionService:
    def __init__(
        self,
        store: SQLiteHarnessStore,
        task_service: TaskService,
        validators: list[PromotionValidator] | None = None,
    ) -> None:
        self.store = store
        self.task_service = task_service
        self.validators = validators or default_promotion_validators()

    def review_task(self, task_id: str, run_id: str | None = None, create_follow_on: bool = True) -> PromotionReviewResult:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        run = self._select_run(task_id, run_id)
        if run.status != RunStatus.COMPLETED:
            raise ValueError(f"Run {run.id} is not promotion-eligible")
        artifacts = self.store.list_artifacts(run.id)
        results = [validator.validate(task, artifacts) for validator in self.validators]
        issues = [issue for result in results for issue in result.issues]
        if not issues:
            promotion = PromotionRecord(
                id=new_id("promotion"),
                task_id=task.id,
                run_id=run.id,
                status=PromotionStatus.APPROVED,
                summary="Promotion review approved the candidate.",
                details={"validators": [self._serialize_result(result) for result in results]},
            )
            self.store.create_promotion(promotion)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=task.id,
                    event_type="promotion_approved",
                    payload={"promotion_id": promotion.id, "run_id": run.id},
                )
            )
            return PromotionReviewResult(promotion=promotion, follow_on_task_id=None)

        follow_on_task_id: str | None = None
        if create_follow_on:
            existing = self.store.find_follow_on_task(task.id, run.id)
            if existing is not None:
                follow_on_task_id = existing.id
            else:
                title, objective = self._follow_on_from_issues(task.title, issues)
                follow_on = self.task_service.create_follow_on_task(
                    parent_task_id=task.id,
                    source_run_id=run.id,
                    title=title,
                    objective=objective,
                )
                follow_on_task_id = follow_on.id

        promotion = PromotionRecord(
            id=new_id("promotion"),
            task_id=task.id,
            run_id=run.id,
            status=PromotionStatus.REJECTED,
            summary="Promotion review rejected the candidate.",
            details={
                "validators": [self._serialize_result(result) for result in results],
                "issue_count": len(issues),
                "follow_on_task_id": follow_on_task_id,
            },
        )
        self.store.create_promotion(promotion)
        self.store.update_task_status(task.id, TaskStatus.FAILED)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="promotion_rejected",
                payload={
                    "promotion_id": promotion.id,
                    "run_id": run.id,
                    "follow_on_task_id": follow_on_task_id,
                },
            )
        )
        return PromotionReviewResult(promotion=promotion, follow_on_task_id=follow_on_task_id)

    def _select_run(self, task_id: str, run_id: str | None):
        if run_id is not None:
            run = self.store.get_run(run_id)
            if run is None or run.task_id != task_id:
                raise ValueError(f"Unknown run {run_id} for task {task_id}")
            return run
        runs = self.store.list_runs(task_id)
        if not runs:
            raise ValueError(f"Task {task_id} has no runs to review")
        return runs[-1]

    def _serialize_result(self, result) -> dict[str, object]:
        return {
            "validator": result.validator,
            "ok": result.ok,
            "summary": result.summary,
            "issues": [
                {
                    "code": issue.code,
                    "summary": issue.summary,
                    "details": issue.details,
                    "follow_on_title": issue.follow_on_title,
                    "follow_on_objective": issue.follow_on_objective,
                }
                for issue in result.issues
            ],
        }

    def _follow_on_from_issues(self, task_title: str, issues: list[ValidationIssue]) -> tuple[str, str]:
        first = issues[0]
        return (
            first.follow_on_title or f"Resolve promotion failure for {task_title}",
            first.follow_on_objective
            or "Address the promotion validation failures recorded for the rejected candidate and regenerate it.",
        )
