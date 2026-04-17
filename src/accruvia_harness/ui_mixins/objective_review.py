"""HarnessUIDataService objective review methods."""
from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any

from ..context_control import objective_execution_gate
from ..domain import (
    ContextRecord, MermaidStatus, Objective, ObjectivePhase, ObjectiveStatus,
    PromotionStatus, Run, RunStatus, Task, TaskStatus, new_id, serialize_dataclass,
)
from ._shared import (
    _OBJECTIVE_REVIEW,
    _OBJECTIVE_REVIEW_DIMENSIONS,
    _OBJECTIVE_REVIEW_PROGRESS,
    _OBJECTIVE_REVIEW_REBUTTAL_OUTCOMES,
    _OBJECTIVE_REVIEW_SEVERITIES,
    _OBJECTIVE_REVIEW_VAGUE_PHRASES,
    _OBJECTIVE_REVIEW_VERDICTS,
)

from ._shared import _to_jsonable

from ..context_control import objective_execution_gate

class ObjectiveReviewMixin:

    def queue_objective_review(self, objective_id: str, *, async_mode: bool = True) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        current = self._objective_review_state(objective_id)
        if current["status"] == "running" and self._objective_review_is_stale(current, objective_id):
            self._mark_objective_review_interrupted(objective, current)
            current = self._objective_review_state(objective_id)
        if current["status"] == "running":
            return {"objective_review_state": current}
        review_summary = self._promotion_review_for_objective(objective_id, [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective_id])
        if not review_summary["ready"]:
            return {"objective_review_state": current}
        if not bool(review_summary.get("can_start_new_round", False)):
            return {"objective_review_state": current}
        review_id = new_id("objective_review")
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_review_started",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Started automatic objective promotion review.",
                metadata={"review_id": review_id},
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Action receipt: Starting automatic objective promotion review.",
                metadata={"kind": "objective_review", "status": "started", "review_id": review_id},
            )
        )
        self._emit_workflow_progress(
            {
                "type": "workflow_stage_changed",
                "stage_kind": "objective_review",
                "stage_status": "started",
                "objective_id": objective.id,
                "objective_title": objective.title,
                "review_id": review_id,
                "detail": "Started automatic objective promotion review.",
            }
        )

        _runner = self.start_objective_lifecycle(objective.id)
        if _runner.phase == ObjectivePhase.EXECUTING:
            _runner._advance(ObjectivePhase.REVIEWING)

        def worker() -> None:
            self._run_objective_review(objective.id, review_id, lifecycle_runner=_runner)

        if async_mode:
            _OBJECTIVE_REVIEW.start(objective.id, worker)
        else:
            worker()
        return {"objective_review_state": self._objective_review_state(objective.id)}


    def _maybe_resume_objective_review(self, objective_id: str) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective_id]
        review_summary = self._promotion_review_for_objective(objective_id, linked_tasks)
        review_state = self._objective_review_state(objective_id)
        if review_state.get("status") == "running" and self._objective_review_is_stale(review_state, objective_id):
            self._mark_objective_review_interrupted(objective, review_state)
            review_summary = self._promotion_review_for_objective(objective_id, linked_tasks)
            review_state = self._objective_review_state(objective_id)
        latest_round = (review_summary.get("review_rounds") or [None])[0]
        latest_round_status = str(latest_round.get("status") or "") if isinstance(latest_round, dict) else ""
        if isinstance(latest_round, dict) and latest_round.get("review_id"):
            review_id = str(latest_round.get("review_id") or "")
            restarted_any = False
            for task in linked_tasks:
                metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
                remediation = metadata.get("objective_review_remediation") if isinstance(metadata.get("objective_review_remediation"), dict) else None
                if (
                    task.status == TaskStatus.FAILED
                    and remediation is not None
                    and str(remediation.get("review_id") or "") == review_id
                ):
                    restarted_any = self._auto_retry_restart_safe_failed_task(task) or restarted_any
            if restarted_any:
                return
        if objective.status != ObjectiveStatus.RESOLVED and latest_round_status not in {"ready_for_rerun", "failed"}:
            return
        if (
            isinstance(latest_round, dict)
            and bool(latest_round.get("needs_remediation"))
            and latest_round.get("review_id")
            and int((latest_round.get("remediation_counts") or {}).get("active", 0) or 0) == 0
            and int((latest_round.get("remediation_counts") or {}).get("pending", 0) or 0) == 0
            and int((latest_round.get("remediation_counts") or {}).get("total", 0) or 0) == 0
        ):
            packets = [
                {
                    "reviewer": str(packet.get("reviewer") or ""),
                    "dimension": str(packet.get("dimension") or ""),
                    "verdict": str(packet.get("verdict") or ""),
                    "summary": str(packet.get("summary") or ""),
                    "findings": list(packet.get("findings") or []),
                }
                for packet in list(latest_round.get("packets") or [])
            ]
            self._create_objective_review_remediation_tasks(objective, str(latest_round.get("review_id") or ""), packets)
            return
        if isinstance(latest_round, dict) and latest_round.get("review_id"):
            self._record_objective_review_worker_responses(objective, latest_round)
        if not review_summary["ready"]:
            return
        if review_state["status"] == "running" and objective_id in _OBJECTIVE_REVIEW._running:
            return
        if not bool(review_summary.get("can_start_new_round", False)):
            return
        self.queue_objective_review(objective_id, async_mode=self._workflow_async_mode())


    def _maybe_auto_promote_on_clear_review(
        self,
        *,
        objective: Objective,
        review_id: str,
        packets: list[dict[str, object]],
    ) -> None:
        """Auto-promote an objective when its review round comes back clear.

        Called by `_run_objective_review` after a review completes with no
        remediation tasks created. The review is "clear" only when every
        reviewer packet carries verdict=='pass'. Any other verdict (concern,
        remediation_required) means remediation tasks were created and we
        would have taken the earlier branch instead of reaching this method.

        Defensive against double-checking: reuses `_promotion_review_for_objective`
        to fetch the authoritative review_clear state so we never promote
        against stale data.

        On success: records `objective_auto_promoted` + `action_receipt`.
        On failure: records `objective_auto_promote_failed` and does NOT
        re-raise. A failed auto-promotion should never block the review's
        own completion handling.
        """
        # Reconfirm clear via the canonical state source — packets alone
        # are not enough; `_promotion_review_for_objective` also checks
        # merge-gate policy, unmerged workspace branches, etc.
        linked_tasks = [
            task
            for task in self.store.list_tasks(objective.project_id)
            if task.objective_id == objective.id
        ]
        promotion_review = self._promotion_review_for_objective(
            objective.id, linked_tasks
        )
        review_clear = bool(promotion_review.get("review_clear"))
        if not review_clear:
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="objective_auto_promote_skipped",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=(
                        "Auto-promotion skipped: review completed without "
                        "remediation tasks but canonical review_clear is False."
                    ),
                    metadata={
                        "review_id": review_id,
                        "reason": "canonical_review_not_clear",
                        "next_action": promotion_review.get("next_action"),
                    },
                )
            )
            return

        try:
            result = self.promote_objective_to_repo(objective.id)
        except Exception as exc:  # noqa: BLE001 — audit-log any failure
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="objective_auto_promote_failed",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=(
                        f"Auto-promotion on review_clear failed: {exc}. "
                        "The operator can retry via the manual promote endpoint."
                    ),
                    metadata={
                        "review_id": review_id,
                        "error": str(exc),
                    },
                )
            )
            self._emit_workflow_progress(
                {
                    "type": "workflow_stage_changed",
                    "stage_kind": "objective_promotion",
                    "stage_status": "failed",
                    "objective_id": objective.id,
                    "objective_title": objective.title,
                    "review_id": review_id,
                    "detail": f"Auto-promotion failed: {exc}",
                }
            )
            return

        # Success — record audit trail
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_auto_promoted",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=(
                    f"Auto-promoted objective after clean review: "
                    f"{result.get('applied_task_count', '?')} task(s) merged to repo."
                ),
                metadata={
                    "review_id": review_id,
                    "trigger": "review_clear",
                    "applied_task_count": result.get("applied_task_count"),
                    "promotion_id": result.get("promotion_id"),
                },
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=(
                    f"Action receipt: Objective auto-promoted on clean review "
                    f"(no operator action required)."
                ),
                metadata={
                    "kind": "objective_promotion",
                    "status": "auto_promoted",
                    "review_id": review_id,
                    "applied_task_count": result.get("applied_task_count"),
                },
            )
        )
        self._emit_workflow_progress(
            {
                "type": "workflow_stage_changed",
                "stage_kind": "objective_promotion",
                "stage_status": "auto_promoted",
                "objective_id": objective.id,
                "objective_title": objective.title,
                "review_id": review_id,
                "detail": (
                    f"Auto-promoted "
                    f"{result.get('applied_task_count', '?')} task(s) to repo "
                    f"after clean review."
                ),
            }
        )


    def _run_objective_review(self, objective_id: str, review_id: str, *, lifecycle_runner=None) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        try:
            linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
            previous_review = self._promotion_review_for_objective(objective_id, linked_tasks)
            prior_rounds = previous_review.get("review_rounds") or []
            for prior_round in prior_rounds:
                if isinstance(prior_round, dict) and prior_round.get("review_id") and str(prior_round.get("review_id") or "") != review_id:
                    self._record_objective_review_worker_responses(objective, prior_round)
                    break
            packets = self._generate_objective_review_packets(objective_id, review_id)
            packet_record_ids: list[str] = []
            for packet in packets:
                packet_record = ContextRecord(
                    id=new_id("context"),
                    record_type="objective_review_packet",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=str(packet["summary"]),
                    metadata={
                        "review_id": review_id,
                        "reviewer": packet["reviewer"],
                        "dimension": packet["dimension"],
                        "verdict": packet["verdict"],
                        "progress_status": packet.get("progress_status"),
                        "severity": packet.get("severity"),
                        "owner_scope": packet.get("owner_scope"),
                        "findings": packet["findings"],
                        "evidence": packet["evidence"],
                        "required_artifact_type": packet.get("required_artifact_type"),
                        "artifact_schema": packet.get("artifact_schema"),
                        "evidence_contract": packet.get("evidence_contract"),
                        "closure_criteria": packet.get("closure_criteria"),
                        "evidence_required": packet.get("evidence_required"),
                        "repeat_reason": packet.get("repeat_reason"),
                        "llm_usage": packet.get("llm_usage"),
                        "llm_usage_reported": packet.get("llm_usage_reported"),
                        "llm_usage_source": packet.get("llm_usage_source"),
                        "backend": packet.get("backend"),
                        "prompt_path": packet.get("prompt_path"),
                        "response_path": packet.get("response_path"),
                        "review_task_id": packet.get("review_task_id"),
                        "review_run_id": packet.get("review_run_id"),
                    },
                )
                self.store.create_context_record(packet_record)
                packet["packet_record_id"] = packet_record.id
                packet_record_ids.append(packet_record.id)
            completed_record = ContextRecord(
                id=new_id("context"),
                record_type="objective_review_completed",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Completed automatic objective review with {len(packets)} reviewer packet(s).",
                metadata={"review_id": review_id, "packet_count": len(packets)},
            )
            self.store.create_context_record(completed_record)
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="action_receipt",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Action receipt: Objective promotion review generated {len(packets)} reviewer packet(s).",
                    metadata={"kind": "objective_review", "status": "completed", "review_id": review_id, "packet_count": len(packets)},
                )
            )
            self._emit_workflow_progress(
                {
                    "type": "workflow_stage_changed",
                    "stage_kind": "objective_review",
                    "stage_status": "completed",
                    "objective_id": objective.id,
                    "objective_title": objective.title,
                    "review_id": review_id,
                    "detail": f"Objective review produced {len(packets)} reviewer packet(s).",
                }
            )
            created_task_ids = self._create_objective_review_remediation_tasks(objective, review_id, packets)
            if created_task_ids:
                self._emit_workflow_progress(
                    {
                        "type": "workflow_stage_changed",
                        "stage_kind": "objective_review",
                        "stage_status": "remediation_created",
                        "objective_id": objective.id,
                        "objective_title": objective.title,
                        "review_id": review_id,
                        "detail": f"Queued {len(created_task_ids)} remediation task(s) from review findings.",
                    }
                )
            self._record_objective_review_cycle_artifact(
                objective=objective,
                review_id=review_id,
                packet_record_ids=packet_record_ids,
                completed_record=completed_record,
                linked_task_ids=created_task_ids,
            )
            self._record_objective_review_reviewer_rebuttals(
                objective=objective,
                review_id=review_id,
                previous_review=previous_review,
                current_packets=packets,
            )
            if created_task_ids:
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="action_receipt",
                        project_id=objective.project_id,
                        objective_id=objective.id,
                        visibility="operator_visible",
                        author_type="system",
                        content=f"Action receipt: Objective review created {len(created_task_ids)} remediation task(s) and returned the objective to Atomic.",
                        metadata={
                            "kind": "objective_review",
                            "status": "remediation_created",
                            "review_id": review_id,
                            "task_ids": created_task_ids,
                        },
                    )
                )
            else:
                # Canonical merge gate: if no remediation was created AND the
                # review came back clear (all 7 dimensions pass), auto-fire
                # objective-level promotion. This restores the full promotion
                # path the hobble in services/run_service.py:642 documented as
                # the preferred alternative to per-task auto-merge.
                #
                # The operator REST endpoint (/api/objectives/{id}/promote)
                # remains available as a manual override, but for a clean
                # review there's no reason to wait for operator action.
                self._maybe_auto_promote_on_clear_review(
                    objective=objective,
                    review_id=review_id,
                    packets=packets,
                )
                if lifecycle_runner is not None and lifecycle_runner.phase == ObjectivePhase.REVIEWING:
                    lifecycle_runner._advance(ObjectivePhase.PROMOTED)
        except Exception as exc:
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="objective_review_failed",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Automatic objective review failed: {exc}",
                    metadata={"review_id": review_id},
                )
            )
            self._emit_workflow_progress(
                {
                    "type": "workflow_stage_changed",
                    "stage_kind": "objective_review",
                    "stage_status": "failed",
                    "objective_id": objective.id,
                    "objective_title": objective.title,
                    "review_id": review_id,
                    "detail": f"Objective review failed: {exc}",
                }
            )


    def _generate_objective_review_packets(self, objective_id: str, review_id: str) -> list[dict[str, object]]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return []
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective_id]
        objective_payload = self._promotion_review_for_objective(objective_id, linked_tasks)
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is not None and getattr(llm_router, "executors", {}):
            from ..services.objective_review_orchestrator import ObjectiveReviewOrchestrator

            skill_registry = self._skill_registry()
            orchestrator = ObjectiveReviewOrchestrator(
                skill_registry=skill_registry,
                llm_router=llm_router,
                store=self.store,
                workspace_root=self.workspace_root,
                telemetry=getattr(self.ctx, "telemetry", None),
            )
            outcome = orchestrator.execute(objective_id=objective.id, review_id=review_id)
            packets = list(outcome.get("packets") or [])
            if packets:
                # Per-skill telemetry is recorded by invoke_skill itself.
                # Compute aggregate usage details from telemetry/diagnostics so
                # downstream UI metadata matches the legacy behaviour.
                llm_usage, usage_reported, usage_source = self._objective_review_usage_details(
                    {},
                    task_id="",
                    run_id="",
                )
                for packet in packets:
                    packet.setdefault("backend", "skills_orchestrator")
                    packet.setdefault("prompt_path", "")
                    packet.setdefault("response_path", "")
                    if not packet.get("llm_usage"):
                        packet["llm_usage"] = llm_usage
                    if not packet.get("llm_usage_reported"):
                        packet["llm_usage_reported"] = usage_reported
                    if not packet.get("llm_usage_source"):
                        packet["llm_usage_source"] = usage_source
                    packet.setdefault("review_task_id", "")
                    packet.setdefault("review_run_id", "")
                return packets
        return self._deterministic_objective_review_packets(objective_payload)


    def _create_objective_review_remediation_tasks(
        self,
        objective: Objective,
        review_id: str,
        packets: list[dict[str, object]],
    ) -> list[str]:
        from ..services.remediation_service import RemediationService
        svc = RemediationService(self.store, self.task_service)
        return svc.create_remediation_tasks(objective, review_id, packets)

    def _objective_review_evidence_contract(self, packet: dict[str, object]) -> dict[str, object]:
        from ..services.remediation_service import RemediationService
        svc = RemediationService(self.store, self.task_service)
        return svc.extract_evidence_contract(packet)

    def _build_objective_review_remediation_objective(
        self,
        *,
        summary: str,
        findings: list[str],
        evidence_contract: dict[str, object],
    ) -> str:
        from ..services.remediation_service import RemediationService
        svc = RemediationService(self.store, self.task_service)
        return svc.build_remediation_objective(
            summary=summary, findings=findings, evidence_contract=evidence_contract,
        )


    def _normalize_objective_review_artifact_schema(
        self,
        raw_schema: object,
        *,
        required_artifact_type: str,
        dimension: str,
    ) -> dict[str, object] | None:
        from ..services.remediation_service import RemediationService
        svc = RemediationService(self.store, self.task_service)
        return svc.normalize_artifact_schema(
            raw_schema,
            required_artifact_type=required_artifact_type,
            dimension=dimension,
        )


    def _default_review_artifact_required_fields(self, artifact_type: str) -> list[str]:
        from ..services.remediation_service import RemediationService
        return RemediationService.default_required_fields(artifact_type)


    def _deterministic_objective_review_packets(self, objective_payload: dict[str, object]) -> list[dict[str, object]]:
        from ..domain import ReviewPacket, ReviewVerdict
        counts = objective_payload.get("task_counts", {}) if isinstance(objective_payload, dict) else {}
        failed = int(counts.get("failed", 0) or 0)
        waived = int(objective_payload.get("waived_failed_count", 0) or 0)
        unresolved = int(objective_payload.get("unresolved_failed_count", 0) or 0)
        _det_usage = {"shared_invocation": True, "reported": False, "missing_reason": "deterministic_review_packet"}
        _disp_schema = {
            "type": "failed_task_disposition_record",
            "description": "Each unresolved failed task must carry an explicit persisted disposition before promotion.",
            "required_fields": ["task_id", "disposition", "rationale"],
        }
        _qa_schema = {
            "type": "objective_review_packet",
            "description": "QA closure requires a persisted review packet that cites the exact completed-task test artifacts.",
            "required_fields": ["review_id", "reviewer", "dimension", "verdict", "artifacts"],
        }
        _waive_schema = {
            "type": "failed_task_disposition_record",
            "description": "Waived failed tasks must retain persisted superseding or waiver rationale.",
            "required_fields": ["task_id", "disposition", "rationale"],
        }
        packets = [
            ReviewPacket(
                reviewer="Intent agent",
                dimension="intent_fidelity",
                verdict=ReviewVerdict.PASS if unresolved == 0 else ReviewVerdict.CONCERN,
                progress_status="not_applicable",
                severity="" if unresolved == 0 else "medium",
                owner_scope="" if unresolved == 0 else "failed task governance",
                summary="Execution completed and the objective reached a resolved state. Review the linked task outcomes against the original intent before promotion.",
                findings=[] if unresolved == 0 else ["There are unresolved failed tasks that still need explicit disposition."],
                evidence=[f"Completed tasks: {int(counts.get('completed', 0) or 0)}", f"Unresolved failed tasks: {unresolved}"],
                required_artifact_type="" if unresolved == 0 else "failed_task_disposition_record",
                artifact_schema={} if unresolved == 0 else _disp_schema,
                evidence_contract={} if unresolved == 0 else {
                    "required_artifact_type": "failed_task_disposition_record",
                    "artifact_schema": _disp_schema,
                    "closure_criteria": "All failed tasks for the objective must be explicitly waived, superseded, or resolved so unresolved failed task count is zero.",
                    "evidence_required": "Objective summary shows zero unresolved failed tasks and records explicit failed-task dispositions.",
                },
                closure_criteria="" if unresolved == 0 else "All failed tasks for the objective must be explicitly waived, superseded, or resolved so unresolved failed task count is zero.",
                evidence_required="" if unresolved == 0 else "Objective summary shows zero unresolved failed tasks and records explicit failed-task dispositions.",
                llm_usage=_det_usage,
                llm_usage_source="deterministic",
            ).to_dict(),
            ReviewPacket(
                reviewer="QA agent",
                dimension="unit_test_coverage",
                verdict=ReviewVerdict.CONCERN,
                progress_status="new_concern",
                severity="medium",
                owner_scope="objective review evidence",
                summary="Unit and integration evidence should be reviewed from the completed task reports before promotion.",
                findings=["Objective-level QA packets are not yet derived from report artifacts."],
                evidence=[f"Historical failed tasks: {failed}", f"Waived failed tasks: {waived}"],
                required_artifact_type="objective_review_packet",
                artifact_schema=_qa_schema,
                evidence_contract={
                    "required_artifact_type": "objective_review_packet",
                    "artifact_schema": _qa_schema,
                    "closure_criteria": "Objective review packets must cite concrete unit-test or integration-test evidence from completed task artifacts for the QA dimensions.",
                    "evidence_required": "A recorded review packet that references completed-task test artifacts and concludes QA pass or resolved concern status.",
                },
                closure_criteria="Objective review packets must cite concrete unit-test or integration-test evidence from completed task artifacts for the QA dimensions.",
                evidence_required="A recorded review packet that references completed-task test artifacts and concludes QA pass or resolved concern status.",
                llm_usage=_det_usage,
                llm_usage_source="deterministic",
            ).to_dict(),
            ReviewPacket(
                reviewer="Structure agent",
                dimension="code_structure",
                verdict=ReviewVerdict.CONCERN if waived else ReviewVerdict.PASS,
                progress_status="new_concern" if waived else "not_applicable",
                severity="medium" if waived else "",
                owner_scope="code structure" if waived else "",
                summary="Historical control-plane failures were waived, so code structure should be reviewed carefully before promotion.",
                findings=["Waived control-plane failures deserve a human review pass."] if waived else [],
                evidence=[f"Waived failed tasks: {waived}"],
                required_artifact_type="" if not waived else "failed_task_disposition_record",
                artifact_schema={} if not waived else _waive_schema,
                evidence_contract={} if not waived else {
                    "required_artifact_type": "failed_task_disposition_record",
                    "artifact_schema": _waive_schema,
                    "closure_criteria": "Historical failed tasks must be superseded or waived with rationale so fragmented partial work is not left unresolved.",
                    "evidence_required": "Failed-task records show explicit superseding or waiver rationale for every historical failure.",
                },
                closure_criteria="Historical failed tasks must be superseded or waived with rationale so fragmented partial work is not left unresolved." if waived else "",
                evidence_required="Failed-task records show explicit superseding or waiver rationale for every historical failure." if waived else "",
                llm_usage=_det_usage,
                llm_usage_source="deterministic",
            ).to_dict(),
        ]
        return packets


    def force_promote_objective_review(self, objective_id: str, *, rationale: str, author: str = "operator") -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        reason = rationale.strip()
        if not reason:
            raise ValueError("A rationale is required to force-promote an objective review")
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
        review = self._promotion_review_for_objective(objective.id, linked_tasks)
        latest_round = (review.get("review_rounds") or [None])[0]
        if not isinstance(latest_round, dict) or not latest_round.get("review_id"):
            raise ValueError("No objective review round exists to override")
        if int(review.get("unresolved_failed_count", 0) or 0) == 0 and bool(review.get("review_clear")):
            return {"objective_id": objective.id, "status": "already_clear"}
        if any(task.status == TaskStatus.ACTIVE for task in linked_tasks):
            raise ValueError("Cannot force-promote while remediation tasks are still active")
        if any(task.status == TaskStatus.PENDING for task in linked_tasks):
            raise ValueError("Cannot force-promote while remediation tasks are still pending")

        review_id = str(latest_round.get("review_id") or "")
        waived_task_ids: list[str] = []
        for task in linked_tasks:
            if task.status != TaskStatus.FAILED:
                continue
            metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
            remediation = metadata.get("objective_review_remediation") if isinstance(metadata.get("objective_review_remediation"), dict) else None
            remediation_review_id = str(remediation.get("review_id") or "") if remediation else ""
            if remediation_review_id and remediation_review_id != review_id:
                continue
            self.task_service.apply_failed_task_disposition(
                task_id=task.id,
                disposition="waive_obsolete",
                rationale=f"Operator force-promoted objective review: {reason}",
            )
            waived_task_ids.append(task.id)

        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_review_override_approved",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="operator",
                content=f"Operator force-approved objective review round {latest_round.get('round_number') or ''}.",
                metadata={
                    "review_id": review_id,
                    "round_number": latest_round.get("round_number"),
                    "rationale": reason,
                    "author": author,
                    "waived_task_ids": waived_task_ids,
                },
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Action receipt: Operator force-approved the latest objective promotion review.",
                metadata={
                    "kind": "objective_review",
                    "status": "force_approved",
                    "review_id": review_id,
                    "rationale": reason,
                    "waived_task_ids": waived_task_ids,
                },
            )
        )
        self.store.update_objective_phase(objective.id)
        return {
            "objective_id": objective.id,
            "status": "force_approved",
            "review_id": review_id,
            "waived_task_ids": waived_task_ids,
        }


    # --- Delegate methods to extracted service classes ---

    def _objective_review_is_stale(self, generation, objective_id=""):
        from ..services.review_state_service import ReviewStateService
        svc = ReviewStateService(self.store, workflow_timing=self.workflow_timing, ctx=self.ctx, emit_progress=self._emit_workflow_progress)
        return svc._objective_review_is_stale(generation, objective_id)

    def _mark_objective_review_interrupted(self, objective, generation):
        from ..services.review_state_service import ReviewStateService
        svc = ReviewStateService(self.store, workflow_timing=self.workflow_timing, ctx=self.ctx, emit_progress=self._emit_workflow_progress)
        return svc._mark_objective_review_interrupted(objective, generation)

    def _objective_review_usage_details(self, diagnostics, *, task_id, run_id):
        from ..services.review_state_service import ReviewStateService
        svc = ReviewStateService(self.store, workflow_timing=self.workflow_timing, ctx=self.ctx, emit_progress=self._emit_workflow_progress)
        return svc._objective_review_usage_details(diagnostics, task_id=task_id, run_id=run_id)

    def _normalize_objective_review_usage_metadata(self, metadata):
        from ..services.review_state_service import ReviewStateService
        svc = ReviewStateService(self.store)
        return svc._normalize_objective_review_usage_metadata(metadata)

    def _record_objective_review_cycle_artifact(self, **kwargs):
        from ..services.review_cycle_recorder import ReviewCycleRecorder
        svc = ReviewCycleRecorder(self.store)
        return svc._record_objective_review_cycle_artifact(**kwargs)

    def _record_objective_review_worker_responses(self, *args, **kwargs):
        from ..services.review_cycle_recorder import ReviewCycleRecorder
        svc = ReviewCycleRecorder(self.store)
        return svc._record_objective_review_worker_responses(*args, **kwargs)

    def _record_objective_review_reviewer_rebuttals(self, *args, **kwargs):
        from ..services.review_cycle_recorder import ReviewCycleRecorder
        svc = ReviewCycleRecorder(self.store)
        return svc._record_objective_review_reviewer_rebuttals(*args, **kwargs)

    def _build_objective_review_prompt(self, *args, **kwargs):
        from ..services.review_prompt_builder import ReviewPromptBuilder
        svc = ReviewPromptBuilder(self.store)
        return svc._build_objective_review_prompt(*args, **kwargs)

    def _parse_objective_review_response(self, *args, **kwargs):
        from ..services.review_prompt_builder import ReviewPromptBuilder
        svc = ReviewPromptBuilder(self.store)
        return svc._parse_objective_review_response(*args, **kwargs)

    def _validate_objective_review_packet(self, *args, **kwargs):
        from ..services.review_prompt_builder import ReviewPromptBuilder
        svc = ReviewPromptBuilder(self.store)
        return svc._validate_objective_review_packet(*args, **kwargs)

    def _objective_round_artifact_is_present(self, *args, **kwargs):
        from ..services.review_prompt_builder import ReviewPromptBuilder
        svc = ReviewPromptBuilder(self.store)
        return svc._objective_round_artifact_is_present(*args, **kwargs)

    def _packet_requests_round_artifact(self, *args, **kwargs):
        from ..services.review_prompt_builder import ReviewPromptBuilder
        svc = ReviewPromptBuilder(self.store)
        return svc._packet_requests_round_artifact(*args, **kwargs)

    def _map_artifact_to_closure(self, *args, **kwargs):
        from ..services.review_cycle_recorder import ReviewCycleRecorder
        svc = ReviewCycleRecorder(self.store)
        return svc._map_artifact_to_closure(*args, **kwargs)

    def _classify_objective_review_rebuttal(self, *args, **kwargs):
        from ..services.review_cycle_recorder import ReviewCycleRecorder
        svc = ReviewCycleRecorder(self.store)
        return svc._classify_objective_review_rebuttal(*args, **kwargs)

    def _promotion_review_for_objective(self, objective_id, linked_tasks):
        from ..services.promotion_review_builder import PromotionReviewBuilder
        builder = PromotionReviewBuilder(self.store, workflow_timing=self.workflow_timing)
        return builder.build(objective_id, linked_tasks)

    def _objective_review_state(self, objective_id):
        from ..services.review_state_service import ReviewStateService
        svc = ReviewStateService(self.store, workflow_timing=self.workflow_timing)
        return svc._objective_review_state(objective_id)
