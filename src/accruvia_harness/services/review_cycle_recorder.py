"""Records review cycle artifacts, worker responses, and reviewer rebuttals."""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from ..domain import ContextRecord, Objective, Task, TaskStatus, new_id, serialize_dataclass
from ..ui_mixins._shared import _OBJECTIVE_REVIEW_REBUTTAL_OUTCOMES


class ReviewCycleRecorder:
    """Extracted from ObjectiveReviewMixin."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def _record_objective_review_cycle_artifact(
        self,
        *,
        objective: Objective,
        review_id: str,
        packet_record_ids: list[str],
        completed_record: ContextRecord,
        linked_task_ids: list[str],
    ) -> None:
        existing = [
            record
            for record in self.store.list_context_records(objective_id=objective.id, record_type="objective_review_cycle_artifact")
            if str(record.metadata.get("review_id") or "") == review_id
        ]
        if existing:
            return
        start_record = next(
            (
                record for record in reversed(self.store.list_context_records(objective_id=objective.id, record_type="objective_review_started"))
                if str(record.metadata.get("review_id") or "") == review_id
            ),
            None,
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_review_cycle_artifact",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Persisted first-class review cycle artifact for review {review_id}.",
                metadata={
                    "review_id": review_id,
                    "start_event": {
                        "record_id": start_record.id if start_record is not None else "",
                        "created_at": start_record.created_at.isoformat() if start_record is not None else "",
                    },
                    "packet_persistence_events": packet_record_ids,
                    "terminal_event": {
                        "record_id": completed_record.id,
                        "created_at": completed_record.created_at.isoformat(),
                    },
                    "linked_outcome": {
                        "kind": "remediation_created" if linked_task_ids else "review_clear",
                        "task_ids": linked_task_ids,
                    },
                },
            )
        )


    def _record_objective_review_worker_responses(self, objective: Objective, latest_round: dict[str, object]) -> None:
        review_id = str(latest_round.get("review_id") or "")
        if not review_id:
            return
        tasks = [
            task for task in self.store.list_tasks(objective.project_id)
            if task.objective_id == objective.id
            and task.strategy == "objective_review_remediation"
            and isinstance(task.external_ref_metadata, dict)
            and isinstance(task.external_ref_metadata.get("objective_review_remediation"), dict)
            and str(task.external_ref_metadata["objective_review_remediation"].get("review_id") or "") == review_id
            and task.status == TaskStatus.COMPLETED
        ]
        existing_keys = {
            (
                str(record.metadata.get("review_id") or ""),
                str(record.metadata.get("task_id") or ""),
                str(record.metadata.get("run_id") or ""),
            )
            for record in self.store.list_context_records(objective_id=objective.id, record_type="objective_review_worker_response")
        }
        for task in tasks:
            metadata = task.external_ref_metadata.get("objective_review_remediation") if isinstance(task.external_ref_metadata.get("objective_review_remediation"), dict) else {}
            runs = self.store.list_runs(task.id)
            run = runs[-1] if runs else None
            run_id = run.id if run is not None else ""
            key = (review_id, task.id, run_id)
            if key in existing_keys:
                continue
            evidence_contract = metadata.get("evidence_contract") if isinstance(metadata.get("evidence_contract"), dict) else {}
            required_artifact_type = str(evidence_contract.get("required_artifact_type") or "")
            artifacts = self.store.list_artifacts(run.id) if run is not None else []
            exact_artifact = next((artifact for artifact in artifacts if artifact.kind == required_artifact_type), artifacts[0] if artifacts else None)
            exact_payload = {
                "artifact_id": exact_artifact.id if exact_artifact is not None else "",
                "kind": exact_artifact.kind if exact_artifact is not None else "",
                "path": exact_artifact.path if exact_artifact is not None else "",
                "summary": exact_artifact.summary if exact_artifact is not None else "",
            }
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="objective_review_worker_response",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    task_id=task.id,
                    run_id=run.id if run is not None else None,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Worker response recorded for review {review_id} {metadata.get('dimension') or ''}.",
                    metadata={
                        "review_id": review_id,
                        "task_id": task.id,
                        "run_id": run.id if run is not None else "",
                        "dimension": str(metadata.get("dimension") or ""),
                        "finding_record_id": str(metadata.get("finding_record_id") or ""),
                        "exact_artifact_produced": exact_payload,
                        "path": exact_payload["path"],
                        "record_id": exact_payload["artifact_id"],
                        "closure_mapping": self._map_artifact_to_closure(evidence_contract, exact_payload),
                        "closure_criteria": str(evidence_contract.get("closure_criteria") or ""),
                        "required_artifact_type": required_artifact_type,
                    },
                )
            )


    def _map_artifact_to_closure(self, evidence_contract: dict[str, object], exact_payload: dict[str, object]) -> str:
        artifact_type = str(evidence_contract.get("required_artifact_type") or "")
        closure = str(evidence_contract.get("closure_criteria") or "")
        path = str(exact_payload.get("path") or "")
        if not path:
            return f"No artifact was found for required artifact type `{artifact_type}`. Closure criteria remain open: {closure}".strip()
        return f"Artifact `{artifact_type}` was produced at {path}. This response maps directly to closure criteria: {closure}".strip()


    def _record_objective_review_reviewer_rebuttals(
        self,
        *,
        objective: Objective,
        review_id: str,
        previous_review: dict[str, object],
        current_packets: list[dict[str, object]],
    ) -> None:
        prior_rounds = list(previous_review.get("review_rounds") or [])
        if not prior_rounds:
            return
        prior_round = prior_rounds[0] if isinstance(prior_rounds[0], dict) else {}
        prior_review_id = str(prior_round.get("review_id") or "")
        if not prior_review_id:
            return
        current_by_dimension = {
            str(packet.get("dimension") or ""): packet
            for packet in current_packets
            if str(packet.get("dimension") or "")
        }
        worker_by_dimension = {
            str(record.metadata.get("dimension") or ""): record
            for record in self.store.list_context_records(objective_id=objective.id, record_type="objective_review_worker_response")
            if str(record.metadata.get("review_id") or "") == prior_review_id and str(record.metadata.get("dimension") or "")
        }
        for packet in list(prior_round.get("packets") or []):
            if str(packet.get("verdict") or "") not in {"concern", "remediation_required"}:
                continue
            dimension = str(packet.get("dimension") or "")
            outcome, reason = self._classify_objective_review_rebuttal(
                packet,
                current_by_dimension.get(dimension),
                worker_by_dimension.get(dimension),
            )
            if outcome not in _OBJECTIVE_REVIEW_REBUTTAL_OUTCOMES:
                continue
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="objective_review_reviewer_rebuttal",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Reviewer rebuttal for {dimension}: {outcome}.",
                    metadata={
                        "review_id": review_id,
                        "prior_review_id": prior_review_id,
                        "dimension": dimension,
                        "outcome": outcome,
                        "reason": reason,
                    },
                )
            )


    def _classify_objective_review_rebuttal(
        self,
        prior_packet: dict[str, object],
        current_packet: dict[str, object] | None,
        worker_response: ContextRecord | None,
    ) -> tuple[str, str]:
        from .remediation_service import RemediationService
        _rem = RemediationService(self.store, None)
        prior_contract = _rem.extract_evidence_contract(prior_packet)
        expected_type = str(prior_contract.get("required_artifact_type") or "")
        if current_packet and str(current_packet.get("verdict") or "") == "pass":
            return "accepted", "Current review packet accepted the evidence and cleared the finding."
        if worker_response is None:
            return "evidence_not_found", "No worker response record was found for the prior finding."
        produced = worker_response.metadata.get("exact_artifact_produced") if isinstance(worker_response.metadata.get("exact_artifact_produced"), dict) else {}
        produced_type = str(produced.get("kind") or "")
        if not str(produced.get("path") or ""):
            return "evidence_not_found", "Worker response did not point to a persisted artifact."
        if expected_type and produced_type and produced_type != expected_type:
            return "wrong_artifact_type", f"Worker produced `{produced_type}` but the contract required `{expected_type}`."
        schema = prior_contract.get("artifact_schema") if isinstance(prior_contract.get("artifact_schema"), dict) else {}
        required_fields = [str(item).strip().lower() for item in list(schema.get("required_fields") or []) if str(item).strip()]
        if any(field in {"terminal_event", "completed_at"} for field in required_fields):
            mapping = str(worker_response.metadata.get("closure_mapping") or "")
            if "No artifact was found" in mapping:
                return "missing_terminal_event", "The required terminal event evidence was not persisted."
        return "artifact_incomplete", "A response artifact exists, but the reviewer still did not accept it as closing the contract."

