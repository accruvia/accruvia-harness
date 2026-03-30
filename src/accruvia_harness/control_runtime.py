from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Callable

from .control_breadcrumbs import BreadcrumbWriter
from .control_classifier import FailureClassifier
from .control_plane import ControlPlane
from .domain import ControlEvent, ControlLaneStateValue, ControlWorkerRun, ObjectiveStatus, new_id
from .store import SQLiteHarnessStore


class ControlRuntimeObserver:
    def __init__(
        self,
        store: SQLiteHarnessStore,
        control_plane: ControlPlane,
        classifier: FailureClassifier,
        breadcrumb_writer: BreadcrumbWriter,
        request_stack_restart: Callable[[dict[str, object]], None] | None = None,
        structural_fix_promotion: Callable[[object, str], dict[str, object]] | None = None,
    ) -> None:
        self.store = store
        self.control_plane = control_plane
        self.classifier = classifier
        self.breadcrumb_writer = breadcrumb_writer
        self.request_stack_restart = request_stack_restart
        self.structural_fix_promotion = structural_fix_promotion

    def handle(self, event: dict[str, object]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "task_started":
            self._set_worker_lane(ControlLaneStateValue.RUNNING, "task_started")
            return
        if event_type == "run_created":
            task_id = str(event["task_id"])
            task = self.store.get_task(task_id)
            self.store.upsert_control_worker_run(
                ControlWorkerRun(
                    id=str(event["run_id"]),
                    task_id=task_id,
                    objective_id=task.objective_id if task is not None else None,
                    attempt=int(event.get("attempt") or 1),
                    status="started",
                )
            )
            self.control_plane.record_budget_usage(budget_scope="worker", budget_key="expensive_coding_runs")
            if self.control_plane.expensive_coding_budget_exhausted():
                self.control_plane.pause_lane("worker", reason="budget_exhausted")
                self.control_plane.record_human_escalation(
                    "budget_exhausted",
                    payload={"reason": "Expensive coding run budget exhausted for the current hour."},
                )
            return
        if event_type == "task_finished":
            run_id = str(event.get("run_id") or "")
            worker_run = self.store.get_control_worker_run(run_id)
            if worker_run is not None:
                self.store.upsert_control_worker_run(
                    replace(
                        worker_run,
                        status=str(event.get("run_status") or "completed"),
                        ended_at=datetime.now(UTC),
                    )
                )
            task_id = str(event.get("task_id") or "")
            completed_task = self.store.get_task(task_id) if task_id else None
            if str(event.get("status") or "") == "completed":
                if completed_task is not None and completed_task.strategy == "sa_structural_fix":
                    if self.structural_fix_promotion is not None and run_id:
                        self.structural_fix_promotion(completed_task, run_id)
                    # Structural repair tasks are the only permitted escape hatch
                    # from a paused worker lane. Normal work resumes only after a
                    # bounded architectural fix completes successfully. The
                    # outer control loop must then restart the mutable app
                    # processes so the newly-written code is what runs next.
                    if self.request_stack_restart is not None:
                        self.request_stack_restart(
                            {
                                "reason": "sa_structural_fix_completed",
                                "task_id": completed_task.id,
                                "objective_id": completed_task.objective_id,
                            }
                        )
                    self.control_plane.resume_lane("worker", reason="sa_structural_fix_completed")
                    self.control_plane.mark_healthy(reason="sa_structural_fix_completed")
                else:
                    self.control_plane.mark_healthy(reason="task_completed")
                    self._enforce_no_progress(completed_task)
            return
        if event_type == "failure_diagnostic":
            self._record_failure(event)
            return
        if event_type == "backends_unavailable":
            classification = self.classifier.classify(str(event.get("message") or ""))
            self._apply_classification_policy(classification.classification, classification.cooldown_seconds)
            return

    def _record_failure(self, event: dict[str, object]) -> None:
        run_id = str(event.get("run_id") or new_id("run"))
        task_id = str(event.get("task_id") or "")
        evidence_lines = [
            str(event.get("failure_category") or ""),
            str(event.get("failure_message") or ""),
            str(event.get("analysis_summary") or ""),
        ]
        classification = self.classifier.classify("\n".join(item for item in evidence_lines if item))
        bundle_dir = self.breadcrumb_writer.write_bundle(
            entity_type="task",
            entity_id=task_id or "unknown_task",
            meta={
                "task_id": task_id,
                "run_id": run_id,
                "attempt": event.get("attempt"),
                "task_status": event.get("task_status"),
                "run_status": event.get("run_status"),
            },
            evidence={
                "failure_category": event.get("failure_category"),
                "failure_message": event.get("failure_message"),
                "analysis_summary": event.get("analysis_summary"),
                "decision": event.get("decision"),
            },
            decision={
                "classification": classification.classification,
                "retry_recommended": classification.retry_recommended,
                "cooldown_seconds": classification.cooldown_seconds,
            },
            worker_run_id=run_id,
            classification=classification.classification,
            summary=f"Task {task_id} failed with {classification.classification}.",
        )
        existing = self.store.get_control_worker_run(run_id)
        if existing is None:
            existing = ControlWorkerRun(id=run_id, task_id=task_id)
        self.store.upsert_control_worker_run(
            replace(
                existing,
                status=str(event.get("run_status") or "failed"),
                classification=classification.classification,
                ended_at=datetime.now(UTC),
                breadcrumb_path=str(bundle_dir),
            )
        )
        self.store.create_control_event(
            ControlEvent(
                id=new_id("control_event"),
                event_type="worker_failed",
                entity_type="task",
                entity_id=task_id or run_id,
                producer="control-runtime",
                payload={
                    "run_id": run_id,
                    "classification": classification.classification,
                    "task_status": event.get("task_status"),
                },
                idempotency_key=new_id("event_key"),
            )
        )
        self._apply_classification_policy(classification.classification, classification.cooldown_seconds)
        if classification.classification == "unknown" and self._recent_classification_count("unknown") >= 2:
            self.control_plane.record_human_escalation(
                "unknown_repeated",
                payload={"reason": "Unknown classification occurred twice and requires operator review."},
            )

    def _set_worker_lane(self, state: ControlLaneStateValue, reason: str) -> None:
        lane = self.store.get_control_lane_state("worker")
        if lane is None:
            return
        self.store.update_control_lane_state(
            replace(lane, state=state, reason=reason, cooldown_until=None, updated_at=datetime.now(UTC))
        )

    def _apply_classification_policy(self, classification: str, cooldown_seconds: int) -> None:
        if classification in {"provider_rate_limit", "provider_outage"} and cooldown_seconds > 0:
            self.control_plane.enter_cooldown("worker", reason=classification, seconds=cooldown_seconds)
            return
        if classification == "credit_exhaustion":
            self.control_plane.pause_lane("worker", reason=classification)
            self.control_plane.record_human_escalation(
                "credit_exhaustion",
                payload={"reason": "Credit exhaustion requires operator action before more work can run."},
            )
            return
        self._set_worker_lane(ControlLaneStateValue.PAUSED, classification)
        self.control_plane.mark_degraded(classification)

    def _recent_classification_count(self, classification: str) -> int:
        count = 0
        for item in self.store.list_control_worker_runs()[:8]:
            if item.classification == classification:
                count += 1
        return count

    def _enforce_no_progress(self, completed_task) -> None:
        if completed_task is None or not completed_task.objective_id:
            return
        objective = self.store.get_objective(completed_task.objective_id)
        if objective is None or objective.status == ObjectiveStatus.RESOLVED:
            return
        recent_runs = [item for item in self.store.list_control_worker_runs() if item.objective_id == completed_task.objective_id][:3]
        if len(recent_runs) < 3:
            return
        if not all(item.status == "completed" for item in recent_runs):
            return
        self.breadcrumb_writer.write_bundle(
            entity_type="objective",
            entity_id=completed_task.objective_id,
            meta={"objective_id": completed_task.objective_id, "task_id": completed_task.id},
            evidence={"recent_completed_runs": [item.id for item in recent_runs], "objective_status": objective.status.value},
            decision={"classification": "no_progress", "retry_recommended": False, "cooldown_seconds": 0},
            classification="no_progress",
            summary=f"Objective {completed_task.objective_id} completed three runs without reaching promotion-ready state.",
        )
        self.control_plane.pause_lane("worker", reason="no_progress")
        self.control_plane.record_human_escalation(
            "no_progress",
            payload={
                "objective_id": completed_task.objective_id,
                "reason": "Three completed coding runs did not advance the objective to a mergeable state.",
            },
        )
