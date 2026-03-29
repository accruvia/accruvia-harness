from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from .control_breadcrumbs import BreadcrumbWriter
from .control_classifier import FailureClassifier
from .control_plane import ControlPlane
from .domain import ControlEvent, ControlLaneStateValue, ControlWorkerRun, new_id
from .store import SQLiteHarnessStore


class ControlRuntimeObserver:
    def __init__(
        self,
        store: SQLiteHarnessStore,
        control_plane: ControlPlane,
        classifier: FailureClassifier,
        breadcrumb_writer: BreadcrumbWriter,
    ) -> None:
        self.store = store
        self.control_plane = control_plane
        self.classifier = classifier
        self.breadcrumb_writer = breadcrumb_writer

    def handle(self, event: dict[str, object]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "task_started":
            self._set_worker_lane(ControlLaneStateValue.RUNNING, "task_started")
            return
        if event_type == "run_created":
            self.store.upsert_control_worker_run(
                ControlWorkerRun(
                    id=str(event["run_id"]),
                    task_id=str(event["task_id"]),
                    attempt=int(event.get("attempt") or 1),
                    status="started",
                )
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
            if str(event.get("status") or "") == "completed":
                self.control_plane.mark_healthy(reason="task_completed")
            return
        if event_type == "failure_diagnostic":
            self._record_failure(event)
            return
        if event_type == "backends_unavailable":
            classification = self.classifier.classify(str(event.get("message") or ""))
            self._set_worker_lane(ControlLaneStateValue.PAUSED, classification.classification)
            self.control_plane.mark_degraded(classification.classification)

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
        self._set_worker_lane(ControlLaneStateValue.PAUSED, classification.classification)
        self.control_plane.mark_degraded(classification.classification)

    def _set_worker_lane(self, state: ControlLaneStateValue, reason: str) -> None:
        lane = self.store.get_control_lane_state("worker")
        if lane is None:
            return
        self.store.update_control_lane_state(
            replace(lane, state=state, reason=reason, cooldown_until=None, updated_at=datetime.now(UTC))
        )
