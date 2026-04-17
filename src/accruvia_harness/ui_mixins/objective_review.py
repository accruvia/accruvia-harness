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


    def _objective_review_is_stale(self, review_state: dict[str, object], objective_id: str = "") -> bool:
        if review_state.get("status") != "running":
            return False
        if objective_id and objective_id in _OBJECTIVE_REVIEW._running:
            return False
        last_activity_at = str(review_state.get("last_activity_at") or "")
        if not last_activity_at:
            return False
        try:
            last_activity = _dt.datetime.fromisoformat(last_activity_at)
        except ValueError:
            return False
        age_seconds = (_dt.datetime.now(_dt.timezone.utc) - last_activity).total_seconds()
        return age_seconds > 300


    def _mark_objective_review_interrupted(self, objective: Objective, review_state: dict[str, object]) -> None:
        review_id = str(review_state.get("review_id") or "")
        if not review_id:
            return
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_review_failed",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Objective promotion review was interrupted before reviewer packets were recorded. The harness can restart the round.",
                metadata={"review_id": review_id, "interrupted": True},
            )
        )
        self._emit_workflow_progress(
            {
                "type": "workflow_stage_changed",
                "stage_kind": "objective_review",
                "stage_status": "interrupted",
                "objective_id": objective.id,
                "objective_title": objective.title,
                "review_id": review_id,
                "detail": "Objective promotion review was interrupted and can restart.",
            }
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Action receipt: Objective promotion review was interrupted and is eligible for restart.",
                metadata={"kind": "objective_review", "status": "interrupted", "review_id": review_id},
            )
        )


    def _objective_review_state(self, objective_id: str) -> dict[str, object]:
        starts = self.store.list_context_records(objective_id=objective_id, record_type="objective_review_started")
        if not starts:
            return {"status": "idle", "review_id": "", "started_at": "", "completed_at": "", "failed_at": "", "last_activity_at": ""}
        start = starts[-1]
        review_id = str(start.metadata.get("review_id") or start.id)
        completed = next(
            (
                record
                for record in reversed(self.store.list_context_records(objective_id=objective_id, record_type="objective_review_completed"))
                if str(record.metadata.get("review_id") or "") == review_id
            ),
            None,
        )
        failed = next(
            (
                record
                for record in reversed(self.store.list_context_records(objective_id=objective_id, record_type="objective_review_failed"))
                if str(record.metadata.get("review_id") or "") == review_id
            ),
            None,
        )
        packets = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="objective_review_packet")
            if str(record.metadata.get("review_id") or "") == review_id
        ]
        status = "running"
        if failed is not None:
            status = "failed"
        elif completed is not None:
            status = "completed"
        related = [start.created_at]
        related.extend(record.created_at for record in packets)
        if completed is not None:
            related.append(completed.created_at)
        if failed is not None:
            related.append(failed.created_at)
        return {
            "status": status,
            "review_id": review_id,
            "started_at": start.created_at.isoformat(),
            "completed_at": completed.created_at.isoformat() if completed is not None else "",
            "failed_at": failed.created_at.isoformat() if failed is not None else "",
            "last_activity_at": max(related).isoformat() if related else "",
            "duration_ms": self.workflow_timing.duration_ms(
                start.created_at,
                completed_at=completed.created_at if completed is not None else None,
                failed_at=failed.created_at if failed is not None else None,
                last_activity_at=max(related) if related else None,
            ),
            "packet_count": len(packets),
            "error": failed.content if failed is not None else "",
        }


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


    def _objective_review_usage_details(
        self,
        diagnostics: dict[str, object],
        *,
        task_id: str,
        run_id: str,
    ) -> tuple[dict[str, object], bool, str]:
        usage = {
            "cost_usd": float(diagnostics.get("cost_usd", 0.0) or 0.0),
            "prompt_tokens": int(diagnostics.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(diagnostics.get("completion_tokens", 0) or 0),
            "total_tokens": int(diagnostics.get("total_tokens", 0) or 0),
            "latency_ms": float(diagnostics.get("latency_ms", 0.0) or 0.0),
            "shared_invocation": True,
        }
        if any(float(usage.get(key, 0) or 0) > 0 for key in ("cost_usd", "prompt_tokens", "completion_tokens", "total_tokens")):
            return usage, True, "diagnostics"
        telemetry = getattr(self.ctx, "telemetry", None)
        if telemetry is not None and hasattr(telemetry, "load_metrics"):
            try:
                metrics = telemetry.load_metrics()
            except Exception:
                metrics = []
            for item in metrics:
                attributes = item.get("attributes") if isinstance(item, dict) else {}
                if not isinstance(attributes, dict):
                    continue
                if str(attributes.get("task_id") or "") != task_id or str(attributes.get("run_id") or "") != run_id:
                    continue
                name = str(item.get("name") or "")
                value = float(item.get("value", 0.0) or 0.0)
                if name == "llm_cost_usd":
                    usage["cost_usd"] = value
                elif name == "llm_prompt_tokens":
                    usage["prompt_tokens"] = int(value)
                elif name == "llm_completion_tokens":
                    usage["completion_tokens"] = int(value)
                elif name == "llm_total_tokens":
                    usage["total_tokens"] = int(value)
                elif name == "llm_execute_duration_ms":
                    usage["latency_ms"] = max(float(usage.get("latency_ms", 0.0) or 0.0), value)
        if any(float(usage.get(key, 0) or 0) > 0 for key in ("cost_usd", "prompt_tokens", "completion_tokens", "total_tokens")):
            return usage, True, "telemetry"
        if float(usage.get("latency_ms", 0.0) or 0.0) > 0:
            usage["reported"] = False
            usage["missing_reason"] = "backend_did_not_report_token_usage"
            return usage, False, "telemetry_latency_only"
        return {
            "shared_invocation": True,
            "reported": False,
            "missing_reason": "backend_did_not_report_token_usage",
        }, False, "unreported"


    def _normalize_objective_review_usage_metadata(
        self,
        metadata: dict[str, object],
    ) -> tuple[dict[str, object], bool, str]:
        usage = dict(metadata.get("llm_usage") or {}) if isinstance(metadata.get("llm_usage"), dict) else {}
        source = str(metadata.get("llm_usage_source") or "").strip()
        raw_reported = metadata.get("llm_usage_reported")
        if isinstance(raw_reported, bool):
            reported = raw_reported
        else:
            reported = True
            if bool(usage.get("shared_invocation")) and not any(
                float(usage.get(key, 0) or 0) > 0 for key in ("cost_usd", "prompt_tokens", "completion_tokens", "total_tokens")
            ):
                reported = False
                if not source:
                    source = "unreported"
                usage.setdefault("reported", False)
                usage.setdefault("missing_reason", "backend_did_not_report_token_usage")
        return usage, reported, source


    def _create_objective_review_remediation_tasks(
        self,
        objective: Objective,
        review_id: str,
        packets: list[dict[str, object]],
    ) -> list[str]:
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
        existing_dimensions = set()
        for task in linked_tasks:
            metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
            remediation = metadata.get("objective_review_remediation") if isinstance(metadata.get("objective_review_remediation"), dict) else None
            if remediation and str(remediation.get("review_id") or "") == review_id:
                existing_dimensions.add(str(remediation.get("dimension") or ""))
        created: list[str] = []
        for packet in packets:
            verdict = str(packet.get("verdict") or "").strip()
            dimension = str(packet.get("dimension") or "").strip()
            if verdict not in {"concern", "remediation_required"} or not dimension or dimension in existing_dimensions:
                continue
            findings = [str(item).strip() for item in list(packet.get("findings") or []) if str(item).strip()]
            summary = str(packet.get("summary") or "").strip()
            evidence_contract = self._objective_review_evidence_contract(packet)
            artifact_type = str(evidence_contract.get("required_artifact_type") or "review_artifact")
            title = f"Produce {artifact_type.replace('_', ' ')} for {dimension.replace('_', ' ')} review finding"
            objective_text = self._build_objective_review_remediation_objective(
                summary=summary,
                findings=findings,
                evidence_contract=evidence_contract,
            )
            task = self.task_service.create_task_with_policy(
                project_id=objective.project_id,
                objective_id=objective.id,
                title=title,
                objective=objective_text,
                priority=objective.priority,
                parent_task_id=None,
                source_run_id=None,
                external_ref_type="objective_review",
                external_ref_id=f"{objective.id}:{review_id}:{dimension}",
                external_ref_metadata={
                    "objective_review_remediation": {
                        "review_id": review_id,
                        "dimension": dimension,
                        "reviewer": str(packet.get("reviewer") or ""),
                        "verdict": verdict,
                        "finding_record_id": str(packet.get("packet_record_id") or ""),
                        "evidence_contract": evidence_contract,
                    }
                },
                validation_profile="generic",
                validation_mode="default_focused",
                scope={},
                strategy="objective_review_remediation",
                max_attempts=3,
                required_artifacts=list(dict.fromkeys(["plan", "report", artifact_type])),
            )
            created.append(task.id)
            existing_dimensions.add(dimension)
        if created:
            self.store.update_objective_phase(objective.id)
        return created


    def _objective_review_evidence_contract(self, packet: dict[str, object]) -> dict[str, object]:
        contract = packet.get("evidence_contract") if isinstance(packet.get("evidence_contract"), dict) else {}
        required_artifact_type = str(
            contract.get("required_artifact_type") or packet.get("required_artifact_type") or ""
        ).strip()
        artifact_schema = self._normalize_objective_review_artifact_schema(
            contract.get("artifact_schema") if contract else packet.get("artifact_schema"),
            required_artifact_type=required_artifact_type,
            dimension=str(packet.get("dimension") or ""),
        ) or {}
        closure_criteria = str(contract.get("closure_criteria") or packet.get("closure_criteria") or "").strip()
        evidence_required = str(contract.get("evidence_required") or packet.get("evidence_required") or "").strip()
        return {
            "required_artifact_type": required_artifact_type,
            "artifact_schema": artifact_schema,
            "closure_criteria": closure_criteria,
            "evidence_required": evidence_required,
        }


    def _build_objective_review_remediation_objective(
        self,
        *,
        summary: str,
        findings: list[str],
        evidence_contract: dict[str, object],
    ) -> str:
        artifact_type = str(evidence_contract.get("required_artifact_type") or "review_artifact")
        artifact_schema = evidence_contract.get("artifact_schema") if isinstance(evidence_contract.get("artifact_schema"), dict) else {}
        required_fields = [str(item).strip() for item in list(artifact_schema.get("required_fields") or []) if str(item).strip()]
        lines = [
            f"A promotion reviewer raised findings that must be addressed before this objective can be promoted.",
            f"Read the findings below carefully. They describe concrete problems the reviewer found in the actual codebase.",
            f"Your job is to FIX the problems described in the findings — write code, refactor, add tests — whatever the findings require.",
            f"After making the fixes, produce a `{artifact_type}` artifact documenting what you changed and proving the closure criteria are met.",
            f"Do NOT fabricate evidence. If the reviewer says a function doesn't exist, you must CREATE it, not write a report claiming it exists.",
        ]
        if summary:
            lines.append(f"Reviewer summary: {summary}")
        if findings:
            lines.append("Findings:")
            lines.extend(f"- {item}" for item in findings)
        if evidence_contract.get("closure_criteria"):
            lines.append(f"Closure criteria: {evidence_contract['closure_criteria']}")
        if evidence_contract.get("evidence_required"):
            lines.append(f"Evidence required: {evidence_contract['evidence_required']}")
        if required_fields:
            lines.append("Artifact schema fields: " + ", ".join(required_fields))
        lines.append("Address the findings FIRST by making real code changes, THEN produce the evidence artifact showing what you did.")
        return "\n".join(lines)


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
        prior_contract = self._objective_review_evidence_contract(prior_packet)
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


    def _build_objective_review_prompt(
        self,
        objective: Objective,
        objective_payload: dict[str, object],
        linked_tasks: list[Task],
    ) -> str:
        intent_model = self.store.latest_intent_model(objective.id)
        tasks_payload = [
            {
                "title": task.title,
                "status": task.status.value,
                "objective": task.objective,
                "strategy": task.strategy,
                "metadata": task.external_ref_metadata,
            }
            for task in linked_tasks
        ]
        prior_rounds = []
        for round_row in list(objective_payload.get("review_rounds") or [])[:3]:
            if not isinstance(round_row, dict):
                continue
            prior_rounds.append(
                {
                    "round_number": round_row.get("round_number"),
                    "status": round_row.get("status"),
                    "verdict_counts": round_row.get("verdict_counts"),
                    "remediation_counts": round_row.get("remediation_counts"),
                    "review_cycle_artifact": round_row.get("review_cycle_artifact"),
                    "worker_responses": round_row.get("worker_responses"),
                    "reviewer_rebuttals": round_row.get("reviewer_rebuttals"),
                    "packets": [
                        {
                            "dimension": packet.get("dimension"),
                            "verdict": packet.get("verdict"),
                            "progress_status": packet.get("progress_status"),
                            "summary": packet.get("summary"),
                            "evidence_contract": packet.get("evidence_contract"),
                        }
                        for packet in list(round_row.get("packets") or [])
                    ],
                }
            )
        return (
            "You are the objective-level promotion review board for the accruvia harness.\n"
            "Review the objective as a whole after execution completed.\n"
            "You may be reviewing a later round after remediation from prior rounds.\n"
            "Judge progress against previous rounds instead of repeating the same concern blindly.\n"
            "Every non-pass packet becomes an Evidence Contract for remediation. Review findings and remediation must speak the same artifact type.\n"
            "Do not treat an actively running review/remediation cycle as proof of failure on its own.\n"
            "If the current lifecycle is still in progress, distinguish missing implementation from missing final evidence.\n"
            "Return JSON only with keys: summary, packets.\n"
            "packets must be an array with EXACTLY 7 packets — one for each dimension listed below. Every dimension MUST appear. If a dimension has no findings, return it with verdict pass.\n"
            "Each packet must contain reviewer, dimension, verdict, progress_status, severity, owner_scope, summary, findings, evidence, required_artifact_type, artifact_schema, closure_criteria, evidence_required.\n"
            "reviewer: short reviewer name\n"
            "dimension: REQUIRED dimensions (all 7 must appear): intent_fidelity, unit_test_coverage, integration_e2e_coverage, security, devops, atomic_fidelity, code_structure\n"
            "verdict: one of pass, concern, remediation_required\n"
            "progress_status: one of new_concern, still_blocking, improving, resolved, not_applicable\n"
            "severity: one of low, medium, high\n"
            "owner_scope: short concrete owner scope such as objective review orchestration, integration tests, promotion apply-back, ui workflow\n"
            "summary: short paragraph\n"
            "findings: array of short strings\n"
            "evidence: array of short strings\n"
            "required_artifact_type: REQUIRED for concern and remediation_required. Must be one of the artifact types the harness can actually produce: "
            "plan, report, test_execution_report, ui_workflow_test_report, ui_workflow_e2e_trace_report, "
            "stale_recovery_test_evidence, completed_task_reconciliation_report, workflow_implementation_evidence_bundle. "
            "Do NOT invent artifact types that are not in this list — the remediation worker can only produce these types.\n"
            "artifact_schema: REQUIRED for concern and remediation_required. JSON object with at least type, description, and required_fields.\n"
            "closure_criteria: REQUIRED for concern and remediation_required. Must be concrete and measurable.\n"
            "evidence_required: REQUIRED for concern and remediation_required. Must name the artifact or proof required to clear the finding.\n"
            "repeat_reason: REQUIRED when verdict is concern or remediation_required and progress_status is improving, still_blocking, or resolved.\n"
            "Reject vague language. Do not say 'improve testing' or 'more evidence' without a measurable closure target.\n"
            "\n"
            "CONVERSATION RULES — this is a dialogue, not a monologue:\n"
            "Previous review rounds include worker_responses from remediation tasks. These are the worker's replies to your evidence contracts. Read them carefully.\n"
            "If a worker produced the artifact you asked for, check whether it satisfies your closure criteria. If it does, mark the dimension resolved/pass.\n"
            "If a worker produced a DIFFERENT artifact type than you demanded, read their response as pushback — they may be telling you the demanded type is not achievable. "
            "Consider whether the substitute artifact adequately demonstrates the same concern is addressed. If so, accept it and move to pass.\n"
            "If a worker completed the task but produced no matching artifact, treat that as the worker saying the demand is infeasible. "
            "Revise your required_artifact_type to something from the producible list above, or accept existing evidence and move to pass.\n"
            "\n"
            "SELF-DOUBT WHEN REPEATING — each round the worker returns the same response to your demand, the probability that YOU are wrong increases:\n"
            "Round 1: State your concern clearly with a concrete evidence contract.\n"
            "Round 2 (same response): The worker may not understand. Rephrase your demand more precisely. Confirm the artifact type exists in the producible list.\n"
            "Round 3+ (same response): You are likely hallucinating a requirement or asking something impossible. "
            "Before repeating still_blocking, search the codebase context for evidence that your demand is achievable. "
            "Include in your repeat_reason a specific, factual argument citing code paths, test files, or artifact schemas that prove your demand is reasonable. "
            "If you cannot make that factual case, you are wrong — revise your demand or accept the worker's evidence and move to pass.\n"
            "The burden of proof shifts to YOU with each repeated round. Argue with facts, not assertions.\n\n"
            f"Objective title: {objective.title}\n"
            f"Objective summary: {objective.summary}\n"
            f"Objective status: {objective.status.value}\n"
            f"Intent summary: {intent_model.intent_summary if intent_model else ''}\n"
            f"Success definition: {intent_model.success_definition if intent_model else ''}\n"
            f"Objective review summary: {json.dumps(objective_payload, indent=2, sort_keys=True)}\n"
            f"Previous review rounds: {json.dumps(prior_rounds, indent=2, sort_keys=True)}\n"
            f"Linked tasks: {json.dumps(tasks_payload, indent=2, sort_keys=True)}\n"
        )


    def _parse_objective_review_response(
        self,
        text: str,
        *,
        objective_payload: dict[str, object] | None = None,
    ) -> list[dict[str, object]] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        candidates.extend(fenced)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            packets = payload.get("packets")
            if not isinstance(packets, list):
                continue
            parsed: list[dict[str, object]] = []
            for item in packets:
                if not isinstance(item, dict):
                    continue
                validated = self._validate_objective_review_packet(item, objective_payload=objective_payload)
                if validated is not None:
                    parsed.append(validated)
            if parsed:
                return parsed
        return None


    def _validate_objective_review_packet(
        self,
        item: dict[str, object],
        *,
        objective_payload: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        reviewer = str(item.get("reviewer") or "").strip()
        dimension = str(item.get("dimension") or "").strip()
        verdict = str(item.get("verdict") or "").strip()
        progress_status = str(item.get("progress_status") or "not_applicable").strip() or "not_applicable"
        summary = str(item.get("summary") or "").strip()
        findings = [str(v).strip() for v in list(item.get("findings") or []) if str(v).strip()]
        evidence = [str(v).strip() for v in list(item.get("evidence") or []) if str(v).strip()]
        severity = str(item.get("severity") or "").strip().lower()
        owner_scope = str(item.get("owner_scope") or "").strip()
        contract_payload = item.get("evidence_contract") if isinstance(item.get("evidence_contract"), dict) else {}
        required_artifact_type = str(
            item.get("required_artifact_type") or contract_payload.get("required_artifact_type") or ""
        ).strip()
        artifact_schema = self._normalize_objective_review_artifact_schema(
            item.get("artifact_schema") if item.get("artifact_schema") is not None else contract_payload.get("artifact_schema"),
            required_artifact_type=required_artifact_type,
            dimension=dimension,
        )
        closure_criteria = str(item.get("closure_criteria") or contract_payload.get("closure_criteria") or "").strip()
        evidence_required = str(item.get("evidence_required") or contract_payload.get("evidence_required") or "").strip()
        repeat_reason = str(item.get("repeat_reason") or "").strip()
        if not reviewer or not summary:
            return None
        if dimension not in _OBJECTIVE_REVIEW_DIMENSIONS:
            return None
        if verdict not in _OBJECTIVE_REVIEW_VERDICTS:
            return None
        if progress_status not in _OBJECTIVE_REVIEW_PROGRESS:
            return None
        if verdict == "pass":
            return {
                "reviewer": reviewer,
                "dimension": dimension,
                "verdict": verdict,
                "progress_status": progress_status,
                "severity": "",
                "owner_scope": "",
                "summary": summary,
                "findings": findings,
                "evidence": evidence,
                "required_artifact_type": "",
                "artifact_schema": {},
                "evidence_contract": {},
                "closure_criteria": "",
                "evidence_required": "",
                "repeat_reason": repeat_reason,
            }
        if severity not in _OBJECTIVE_REVIEW_SEVERITIES:
            return None
        if not owner_scope or not closure_criteria or not evidence_required or not required_artifact_type or artifact_schema is None:
            return None
        if progress_status in {"improving", "still_blocking", "resolved"} and not repeat_reason:
            return None
        if not findings or not evidence:
            return None
        lowered_closure = closure_criteria.lower()
        lowered_evidence_required = evidence_required.lower()
        if not any(
            marker in lowered_closure
            for marker in ("must", "shows", "show", "recorded", "exists", "complete", "passes", "pass", "zero", "all ", "at least", "no ")
        ):
            return None
        if any(phrase in lowered_closure for phrase in _OBJECTIVE_REVIEW_VAGUE_PHRASES):
            return None
        if any(phrase in lowered_evidence_required for phrase in ("more evidence", "stronger evidence", "better tests", "improve")):
            return None
        if (
            objective_payload
            and progress_status in {"improving", "still_blocking", "resolved"}
            and self._objective_round_artifact_is_present(objective_payload)
            and self._packet_requests_round_artifact(evidence_required, closure_criteria)
        ):
            return None
        evidence_contract = {
            "required_artifact_type": required_artifact_type,
            "artifact_schema": artifact_schema,
            "closure_criteria": closure_criteria,
            "evidence_required": evidence_required,
        }
        return {
            "reviewer": reviewer,
            "dimension": dimension,
            "verdict": verdict,
            "progress_status": progress_status,
            "severity": severity,
            "owner_scope": owner_scope,
            "summary": summary,
            "findings": findings,
            "evidence": evidence,
            "required_artifact_type": required_artifact_type,
            "artifact_schema": artifact_schema,
            "evidence_contract": evidence_contract,
            "closure_criteria": closure_criteria,
            "evidence_required": evidence_required,
            "repeat_reason": repeat_reason,
        }


    def _objective_round_artifact_is_present(self, objective_payload: dict[str, object]) -> bool:
        rounds = list(objective_payload.get("review_rounds") or [])
        if not rounds:
            return False
        latest = rounds[0] if isinstance(rounds[0], dict) else {}
        if not latest:
            return False
        cycle_artifact = latest.get("review_cycle_artifact") if isinstance(latest.get("review_cycle_artifact"), dict) else {}
        if cycle_artifact:
            return bool(cycle_artifact.get("record_id")) and bool(cycle_artifact.get("terminal_event"))
        packet_count = int(latest.get("packet_count") or 0)
        completed_at = str(latest.get("completed_at") or "")
        verdict_counts = latest.get("verdict_counts") if isinstance(latest.get("verdict_counts"), dict) else {}
        remediation_counts = latest.get("remediation_counts") if isinstance(latest.get("remediation_counts"), dict) else {}
        terminal_branch_present = (
            str(latest.get("status") or "") == "passed"
            or int(remediation_counts.get("total", 0) or 0) > 0
        )
        return bool(completed_at) and packet_count >= 7 and sum(int(verdict_counts.get(k, 0) or 0) for k in ("pass", "concern", "remediation_required")) > 0 and terminal_branch_present


    def _packet_requests_round_artifact(self, evidence_required: str, closure_criteria: str) -> bool:
        text = f"{evidence_required}\n{closure_criteria}".lower()
        markers = (
            "completed objective review",
            "completed objective review run artifact",
            "persisted objective review artifact",
            "completed end-to-end objective review",
            "completed objective review cycle",
            "completed round",
            "terminal round state",
            "completed_at",
            "persisted reviewer packets",
            "review start",
            "terminal review",
            "review approval",
            "remediation linkage",
        )
        return any(marker in text for marker in markers)


    def _normalize_objective_review_artifact_schema(
        self,
        raw_schema: object,
        *,
        required_artifact_type: str,
        dimension: str,
    ) -> dict[str, object] | None:
        artifact_type = required_artifact_type.strip()
        if not artifact_type:
            return None
        schema: dict[str, object] = {}
        if isinstance(raw_schema, dict):
            schema = dict(raw_schema)
        elif isinstance(raw_schema, str) and raw_schema.strip():
            schema = {"description": raw_schema.strip()}
        required_fields = [str(item).strip() for item in list(schema.get("required_fields") or []) if str(item).strip()]
        if not required_fields:
            required_fields = self._default_review_artifact_required_fields(artifact_type)
        description = str(schema.get("description") or "").strip()
        if not description:
            description = f"Persist one {artifact_type} artifact for the {dimension or 'objective review'} dimension."
        normalized = {
            "type": str(schema.get("type") or artifact_type).strip() or artifact_type,
            "description": description,
            "required_fields": required_fields,
        }
        if schema.get("record_locator"):
            normalized["record_locator"] = schema.get("record_locator")
        return normalized


    def _default_review_artifact_required_fields(self, artifact_type: str) -> list[str]:
        lowered = artifact_type.lower()
        if "review_cycle" in lowered or "telemetry" in lowered:
            return ["review_id", "start_event", "packet_persistence_events", "terminal_event", "linked_outcome"]
        if "review_packet" in lowered:
            return ["review_id", "reviewer", "dimension", "verdict", "artifacts"]
        if "test" in lowered:
            return ["artifact_path", "test_targets", "result"]
        return ["artifact_path", "summary"]


    def _deterministic_objective_review_packets(self, objective_payload: dict[str, object]) -> list[dict[str, object]]:
        counts = objective_payload.get("task_counts", {}) if isinstance(objective_payload, dict) else {}
        failed = int(counts.get("failed", 0) or 0)
        waived = int(objective_payload.get("waived_failed_count", 0) or 0)
        unresolved = int(objective_payload.get("unresolved_failed_count", 0) or 0)
        packets = [
            {
                "reviewer": "Intent agent",
                "dimension": "intent_fidelity",
                "verdict": "pass" if unresolved == 0 else "concern",
                "progress_status": "not_applicable",
                "severity": "" if unresolved == 0 else "medium",
                "owner_scope": "" if unresolved == 0 else "failed task governance",
                "summary": "Execution completed and the objective reached a resolved state. Review the linked task outcomes against the original intent before promotion.",
                "findings": [] if unresolved == 0 else ["There are unresolved failed tasks that still need explicit disposition."],
                "evidence": [f"Completed tasks: {int(counts.get('completed', 0) or 0)}", f"Unresolved failed tasks: {unresolved}"],
                "required_artifact_type": "" if unresolved == 0 else "failed_task_disposition_record",
                "artifact_schema": {} if unresolved == 0 else {
                    "type": "failed_task_disposition_record",
                    "description": "Each unresolved failed task must carry an explicit persisted disposition before promotion.",
                    "required_fields": ["task_id", "disposition", "rationale"],
                },
                "evidence_contract": {} if unresolved == 0 else {
                    "required_artifact_type": "failed_task_disposition_record",
                    "artifact_schema": {
                        "type": "failed_task_disposition_record",
                        "description": "Each unresolved failed task must carry an explicit persisted disposition before promotion.",
                        "required_fields": ["task_id", "disposition", "rationale"],
                    },
                    "closure_criteria": "All failed tasks for the objective must be explicitly waived, superseded, or resolved so unresolved failed task count is zero.",
                    "evidence_required": "Objective summary shows zero unresolved failed tasks and records explicit failed-task dispositions.",
                },
                "closure_criteria": "" if unresolved == 0 else "All failed tasks for the objective must be explicitly waived, superseded, or resolved so unresolved failed task count is zero.",
                "evidence_required": "" if unresolved == 0 else "Objective summary shows zero unresolved failed tasks and records explicit failed-task dispositions.",
                "repeat_reason": "",
                "llm_usage": {"shared_invocation": True, "reported": False, "missing_reason": "deterministic_review_packet"},
                "llm_usage_reported": False,
                "llm_usage_source": "deterministic",
            },
            {
                "reviewer": "QA agent",
                "dimension": "unit_test_coverage",
                "verdict": "concern",
                "progress_status": "new_concern",
                "severity": "medium",
                "owner_scope": "objective review evidence",
                "summary": "Unit and integration evidence should be reviewed from the completed task reports before promotion.",
                "findings": ["Objective-level QA packets are not yet derived from report artifacts."],
                "evidence": [f"Historical failed tasks: {failed}", f"Waived failed tasks: {waived}"],
                "required_artifact_type": "objective_review_packet",
                "artifact_schema": {
                    "type": "objective_review_packet",
                    "description": "QA closure requires a persisted review packet that cites the exact completed-task test artifacts.",
                    "required_fields": ["review_id", "reviewer", "dimension", "verdict", "artifacts"],
                },
                "evidence_contract": {
                    "required_artifact_type": "objective_review_packet",
                    "artifact_schema": {
                        "type": "objective_review_packet",
                        "description": "QA closure requires a persisted review packet that cites the exact completed-task test artifacts.",
                        "required_fields": ["review_id", "reviewer", "dimension", "verdict", "artifacts"],
                    },
                    "closure_criteria": "Objective review packets must cite concrete unit-test or integration-test evidence from completed task artifacts for the QA dimensions.",
                    "evidence_required": "A recorded review packet that references completed-task test artifacts and concludes QA pass or resolved concern status.",
                },
                "closure_criteria": "Objective review packets must cite concrete unit-test or integration-test evidence from completed task artifacts for the QA dimensions.",
                "evidence_required": "A recorded review packet that references completed-task test artifacts and concludes QA pass or resolved concern status.",
                "repeat_reason": "",
                "llm_usage": {"shared_invocation": True, "reported": False, "missing_reason": "deterministic_review_packet"},
                "llm_usage_reported": False,
                "llm_usage_source": "deterministic",
            },
            {
                "reviewer": "Structure agent",
                "dimension": "code_structure",
                "verdict": "concern" if waived else "pass",
                "progress_status": "new_concern" if waived else "not_applicable",
                "severity": "medium" if waived else "",
                "owner_scope": "code structure" if waived else "",
                "summary": "Historical control-plane failures were waived, so code structure should be reviewed carefully before promotion.",
                "findings": ["Waived control-plane failures deserve a human review pass."] if waived else [],
                "evidence": [f"Waived failed tasks: {waived}"],
                "required_artifact_type": "" if not waived else "failed_task_disposition_record",
                "artifact_schema": {} if not waived else {
                    "type": "failed_task_disposition_record",
                    "description": "Waived failed tasks must retain persisted superseding or waiver rationale.",
                    "required_fields": ["task_id", "disposition", "rationale"],
                },
                "evidence_contract": {} if not waived else {
                    "required_artifact_type": "failed_task_disposition_record",
                    "artifact_schema": {
                        "type": "failed_task_disposition_record",
                        "description": "Waived failed tasks must retain persisted superseding or waiver rationale.",
                        "required_fields": ["task_id", "disposition", "rationale"],
                    },
                    "closure_criteria": "Historical failed tasks must be superseded or waived with rationale so fragmented partial work is not left unresolved.",
                    "evidence_required": "Failed-task records show explicit superseding or waiver rationale for every historical failure.",
                },
                "closure_criteria": "Historical failed tasks must be superseded or waived with rationale so fragmented partial work is not left unresolved." if waived else "",
                "evidence_required": "Failed-task records show explicit superseding or waiver rationale for every historical failure." if waived else "",
                "repeat_reason": "",
                "llm_usage": {"shared_invocation": True, "reported": False, "missing_reason": "deterministic_review_packet"},
                "llm_usage_reported": False,
                "llm_usage_source": "deterministic",
            },
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


    def harness_atomicity_overview(self) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        for project in self.store.list_projects():
            objectives = self.store.list_objectives(project.id)
            tasks = self.store.list_tasks(project.id)
            tasks_by_objective = {
                objective.id: [task for task in tasks if task.objective_id == objective.id]
                for objective in objectives
            }
            for objective in objectives:
                linked_tasks = tasks_by_objective.get(objective.id, [])
                workflow = self._harness_workflow_status_for_objective(objective, linked_tasks)
                gate = objective_execution_gate(self.store, objective.id)
                generation = self._atomic_generation_state(objective.id)
                review = self._promotion_review_for_objective(objective.id, linked_tasks)
                task_counts = {"completed": 0, "active": 0, "failed": 0, "pending": 0}
                latest_activity = objective.updated_at.isoformat() if objective.updated_at else ""
                for task in linked_tasks:
                    status = task.status.value if hasattr(task.status, "value") else str(task.status)
                    if status in task_counts:
                        task_counts[status] += 1
                    if task.updated_at and task.updated_at.isoformat() > latest_activity:
                        latest_activity = task.updated_at.isoformat()
                rows.append(
                    {
                        "id": objective.id,
                        "project_id": project.id,
                        "project_name": project.name,
                        "title": objective.title,
                        "status": objective.status.value,
                        "workflow": workflow,
                        "execution_gate": {
                            "ready": gate.ready,
                            "checks": _to_jsonable(gate.gate_checks),
                        },
                        "atomic_generation": generation,
                        "task_counts": task_counts,
                        "unresolved_failed_count": int(review.get("unresolved_failed_count") or 0),
                        "waived_failed_count": int(review.get("waived_failed_count") or 0),
                        "failed_tasks": list(review.get("failed_tasks") or []),
                        "task_total": sum(task_counts.values()),
                        "latest_activity_at": latest_activity,
                    }
                )
        rows.sort(
            key=lambda item: (
                int((item.get("task_counts") or {}).get("active", 0)),
                int((item.get("task_counts") or {}).get("pending", 0)),
                str(item.get("latest_activity_at") or ""),
                str(item.get("title") or ""),
            ),
            reverse=True,
        )
        return {"objectives": rows}


    def harness_promotion_overview(self) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        for project in self.store.list_projects():
            objectives = self.store.list_objectives(project.id)
            tasks = self.store.list_tasks(project.id)
            tasks_by_objective = {
                objective.id: [task for task in tasks if task.objective_id == objective.id]
                for objective in objectives
            }
            for objective in objectives:
                linked_tasks = tasks_by_objective.get(objective.id, [])
                review = self._promotion_review_for_objective(objective.id, linked_tasks)
                latest_round = (review.get("review_rounds") or [None])[0]
                rows.append(
                    {
                        "id": objective.id,
                        "project_id": project.id,
                        "project_name": project.name,
                        "title": objective.title,
                        "status": objective.status.value,
                        "review_clear": bool(review.get("review_clear")),
                        "next_action": str(review.get("next_action") or ""),
                        "phase": str(review.get("phase") or ""),
                        "review_round_count": len(review.get("review_rounds") or []),
                        "review_packet_count": int(
                            review.get("review_packet_count")
                            or review.get("objective_review_packet_count")
                            or 0
                        ),
                        "unresolved_failed_count": int(review.get("unresolved_failed_count") or 0),
                        "waived_failed_count": int(review.get("waived_failed_count") or 0),
                        "latest_round": latest_round,
                    }
                )
        rows.sort(
            key=lambda item: (
                bool(item.get("review_clear")),
                -int(item.get("unresolved_failed_count") or 0),
                -int(item.get("review_round_count") or 0),
                str(((item.get("latest_round") or {}) if isinstance(item.get("latest_round"), dict) else {}).get("last_activity_at") or ""),
                str(item.get("title") or ""),
            )
        )
        return {"objectives": rows}

    def _promotion_review_for_objective(
        self,
        objective_id: str,
        linked_tasks: list[Task],
    ) -> dict[str, object]:
        objective_review_state = self._objective_review_state(objective_id)
        promotions_by_task = {
            task.id: [serialize_dataclass(promotion) for promotion in self.store.list_promotions(task.id)]
            for task in linked_tasks
        }
        tasks_by_id = {task.id: task for task in linked_tasks}
        objective_records = self.store.list_context_records(objective_id=objective_id)
        review_start_records = [record for record in objective_records if record.record_type == "objective_review_started"]
        review_completed_records = [record for record in objective_records if record.record_type == "objective_review_completed"]
        review_failed_records = [record for record in objective_records if record.record_type == "objective_review_failed"]
        review_packet_records = [record for record in objective_records if record.record_type == "objective_review_packet"]
        review_cycle_artifact_records = [record for record in objective_records if record.record_type == "objective_review_cycle_artifact"]
        worker_response_records = [record for record in objective_records if record.record_type == "objective_review_worker_response"]
        reviewer_rebuttal_records = [record for record in objective_records if record.record_type == "objective_review_reviewer_rebuttal"]
        override_records = [record for record in objective_records if record.record_type == "objective_review_override_approved"]
        waivers_by_task: dict[str, dict[str, object]] = {}
        for record in objective_records:
            if record.record_type != "failed_task_waived":
                continue
            task_id = str(record.metadata.get("task_id") or "")
            if not task_id:
                continue
            waivers_by_task[task_id] = {
                "record_id": record.id,
                "rationale": record.content,
                "created_at": record.created_at.isoformat(),
                "disposition": record.metadata.get("disposition"),
            }
        counts = {"completed": 0, "active": 0, "pending": 0, "failed": 0}
        for task in linked_tasks:
            status = task.status.value
            if status in counts:
                counts[status] += 1
        promotion_started = any(
            [
                review_start_records,
                review_completed_records,
                review_failed_records,
                review_packet_records,
                review_cycle_artifact_records,
                worker_response_records,
                reviewer_rebuttal_records,
                override_records,
            ]
        )
        failed_entries: list[dict[str, object]] = []
        unresolved_failed_count = 0
        waived_failed_count = 0
        historical_failed_count = 0
        for task in linked_tasks:
            if task.status.value != "failed":
                continue
            waiver = waivers_by_task.get(task.id)
            metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
            disposition = metadata.get("failed_task_disposition") if isinstance(metadata.get("failed_task_disposition"), dict) else None
            if waiver or (disposition and str(disposition.get("kind") or "") == "waive_obsolete"):
                effective_status = "waived"
            elif promotion_started:
                effective_status = "historical"
            else:
                effective_status = "blocking"
            if effective_status == "waived":
                waived_failed_count += 1
            elif effective_status == "historical":
                historical_failed_count += 1
            else:
                unresolved_failed_count += 1
            failed_entries.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "objective": task.objective,
                    "status": task.status.value,
                    "effective_status": effective_status,
                    "disposition": disposition,
                    "waiver": waiver,
                }
            )
        review_packets: list[dict[str, object]] = []
        for task in linked_tasks:
            promotions = promotions_by_task.get(task.id) or []
            if not promotions:
                continue
            latest = promotions[-1]
            validators = latest.get("details", {}).get("validators", []) if isinstance(latest.get("details"), dict) else []
            issues = [
                issue
                for validator in validators if isinstance(validator, dict)
                for issue in validator.get("issues", [])
                if isinstance(issue, dict)
            ]
            review_packets.append(
                {
                    "source": "task_promotion",
                    "task_id": task.id,
                    "task_title": task.title,
                    "task_status": task.status.value,
                    "latest": latest,
                    "all": promotions,
                    "issue_count": len(issues),
                }
            )
        ready = counts["active"] == 0 and counts["pending"] == 0 and unresolved_failed_count == 0
        remediation_tasks_by_review: dict[str, list[Task]] = {}
        for task in linked_tasks:
            metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
            remediation = metadata.get("objective_review_remediation") if isinstance(metadata.get("objective_review_remediation"), dict) else None
            review_id = str(remediation.get("review_id") or "") if remediation else ""
            if not review_id:
                continue
            remediation_tasks_by_review.setdefault(review_id, []).append(task)
        round_rows: list[dict[str, object]] = []
        start_order = sorted(review_start_records, key=lambda record: record.created_at)
        for idx, start in enumerate(start_order, start=1):
            review_id = str(start.metadata.get("review_id") or start.id)
            packets = []
            for record in review_packet_records:
                if str(record.metadata.get("review_id") or "") != review_id:
                    continue
                llm_usage, llm_usage_reported, llm_usage_source = self._normalize_objective_review_usage_metadata(record.metadata)
                packets.append(
                    {
                        "source": "objective_review",
                        "review_id": review_id,
                        "reviewer": str(record.metadata.get("reviewer") or ""),
                        "dimension": str(record.metadata.get("dimension") or ""),
                        "verdict": str(record.metadata.get("verdict") or ""),
                        "progress_status": str(record.metadata.get("progress_status") or "not_applicable"),
                        "severity": str(record.metadata.get("severity") or ""),
                        "owner_scope": str(record.metadata.get("owner_scope") or ""),
                        "summary": record.content,
                        "findings": list(record.metadata.get("findings") or []),
                        "evidence": list(record.metadata.get("evidence") or []),
                        "required_artifact_type": str(record.metadata.get("required_artifact_type") or ""),
                        "artifact_schema": record.metadata.get("artifact_schema") if isinstance(record.metadata.get("artifact_schema"), dict) else {},
                        "evidence_contract": self._objective_review_evidence_contract(record.metadata),
                        "closure_criteria": str(record.metadata.get("closure_criteria") or ""),
                        "evidence_required": str(record.metadata.get("evidence_required") or ""),
                        "repeat_reason": str(record.metadata.get("repeat_reason") or ""),
                        "llm_usage": llm_usage,
                        "llm_usage_reported": llm_usage_reported,
                        "llm_usage_source": llm_usage_source,
                        "backend": record.metadata.get("backend"),
                        "created_at": record.created_at.isoformat(),
                    }
                )
            completed = next(
                (record for record in reversed(review_completed_records) if str(record.metadata.get("review_id") or "") == review_id),
                None,
            )
            failed = next(
                (record for record in reversed(review_failed_records) if str(record.metadata.get("review_id") or "") == review_id),
                None,
            )
            verdict_counts = {"pass": 0, "concern": 0, "remediation_required": 0}
            for packet in packets:
                verdict = str(packet.get("verdict") or "")
                if verdict in verdict_counts:
                    verdict_counts[verdict] += 1
            remediation_tasks = remediation_tasks_by_review.get(review_id, [])
            review_cycle_artifact = next(
                (
                    record for record in reversed(review_cycle_artifact_records)
                    if str(record.metadata.get("review_id") or "") == review_id
                ),
                None,
            )
            worker_responses = [
                {
                    "record_id": record.id,
                    "task_id": str(record.metadata.get("task_id") or ""),
                    "run_id": str(record.metadata.get("run_id") or ""),
                    "dimension": str(record.metadata.get("dimension") or ""),
                    "finding_record_id": str(record.metadata.get("finding_record_id") or ""),
                    "exact_artifact_produced": record.metadata.get("exact_artifact_produced"),
                    "closure_mapping": str(record.metadata.get("closure_mapping") or ""),
                    "created_at": record.created_at.isoformat(),
                }
                for record in worker_response_records
                if str(record.metadata.get("review_id") or "") == review_id
            ]
            reviewer_rebuttals = [
                {
                    "record_id": record.id,
                    "prior_review_id": str(record.metadata.get("prior_review_id") or ""),
                    "dimension": str(record.metadata.get("dimension") or ""),
                    "outcome": str(record.metadata.get("outcome") or ""),
                    "reason": str(record.metadata.get("reason") or ""),
                    "created_at": record.created_at.isoformat(),
                }
                for record in reviewer_rebuttal_records
                if str(record.metadata.get("review_id") or "") == review_id
            ]
            operator_override = next(
                (
                    record for record in reversed(override_records)
                    if str(record.metadata.get("review_id") or "") == review_id
                ),
                None,
            )
            remediation_counts = {"total": len(remediation_tasks), "completed": 0, "active": 0, "pending": 0, "failed": 0}
            for task in remediation_tasks:
                effective = task.status.value
                if effective == "failed":
                    metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
                    disposition = metadata.get("failed_task_disposition") if isinstance(metadata.get("failed_task_disposition"), dict) else None
                    if disposition and str(disposition.get("kind") or "") == "waive_obsolete":
                        effective = "completed"
                if effective in remediation_counts:
                    remediation_counts[effective] += 1
            needs_remediation = verdict_counts["concern"] > 0 or verdict_counts["remediation_required"] > 0
            status = "running"
            if failed is not None:
                status = "failed"
            elif completed is not None:
                if needs_remediation:
                    if remediation_counts["active"] > 0 or remediation_counts["pending"] > 0:
                        status = "remediating"
                    elif remediation_counts["total"] > 0 and remediation_counts["failed"] == 0 and remediation_counts["completed"] == remediation_counts["total"]:
                        status = "ready_for_rerun"
                    else:
                        status = "needs_remediation"
                else:
                    status = "passed"
            if operator_override is not None:
                status = "passed"
            round_activity = [start.created_at]
            round_activity.extend(record.created_at for record in review_packet_records if str(record.metadata.get("review_id") or "") == review_id)
            if completed is not None:
                round_activity.append(completed.created_at)
            if failed is not None:
                round_activity.append(failed.created_at)
            round_rows.append(
                {
                    "review_id": review_id,
                    "round_number": idx,
                    "status": status,
                    "started_at": start.created_at.isoformat(),
                    "completed_at": completed.created_at.isoformat() if completed is not None else "",
                    "failed_at": failed.created_at.isoformat() if failed is not None else "",
                    "last_activity_at": max(round_activity).isoformat() if round_activity else "",
                    "duration_ms": self.workflow_timing.duration_ms(
                        start.created_at,
                        completed_at=completed.created_at if completed is not None else None,
                        failed_at=failed.created_at if failed is not None else None,
                        last_activity_at=max(round_activity) if round_activity else None,
                    ),
                    "packet_count": len(packets),
                    "verdict_counts": verdict_counts,
                    "packets": sorted(
                        packets,
                        key=lambda item: (str(item.get("created_at") or ""), str(item.get("dimension") or "")),
                        reverse=True,
                    ),
                    "review_cycle_artifact": {
                        "record_id": review_cycle_artifact.id,
                        "start_event": review_cycle_artifact.metadata.get("start_event"),
                        "packet_persistence_events": list(review_cycle_artifact.metadata.get("packet_persistence_events") or []),
                        "terminal_event": review_cycle_artifact.metadata.get("terminal_event"),
                        "linked_outcome": review_cycle_artifact.metadata.get("linked_outcome"),
                    } if review_cycle_artifact is not None else {},
                    "operator_override": {
                        "record_id": operator_override.id,
                        "rationale": str(operator_override.metadata.get("rationale") or operator_override.content or ""),
                        "author": str(operator_override.metadata.get("author") or operator_override.author_type or "operator"),
                        "created_at": operator_override.created_at.isoformat(),
                        "waived_task_ids": list(operator_override.metadata.get("waived_task_ids") or []),
                    } if operator_override is not None else {},
                    "worker_responses": sorted(worker_responses, key=lambda item: str(item.get("created_at") or ""), reverse=True),
                    "reviewer_rebuttals": sorted(reviewer_rebuttals, key=lambda item: str(item.get("created_at") or ""), reverse=True),
                    "remediation_counts": remediation_counts,
                    "remediation_tasks": [
                        {"id": task.id, "title": task.title, "status": task.status.value}
                        for task in sorted(remediation_tasks, key=lambda item: item.created_at)
                    ],
                    "needs_remediation": needs_remediation,
                }
            )
        review_rounds = sorted(round_rows, key=lambda item: int(item.get("round_number") or 0), reverse=True)
        latest_round = review_rounds[0] if review_rounds else None
        latest_override = (
            latest_round.get("operator_override")
            if isinstance(latest_round, dict) and isinstance(latest_round.get("operator_override"), dict)
            else {}
        )
        objective_review_packets = list(latest_round.get("packets") or []) if isinstance(latest_round, dict) else []
        all_review_packets = objective_review_packets + review_packets
        all_review_packets.sort(
            key=lambda item: (
                str(
                    item.get("created_at")
                    or (item.get("latest") or {}).get("created_at")
                    or ""
                ),
                str(item.get("task_title") or item.get("reviewer") or ""),
            ),
            reverse=True,
        )
        verdict_counts = {"pass": 0, "concern": 0, "remediation_required": 0}
        for packet in objective_review_packets:
            verdict = str(packet.get("verdict") or "").strip()
            if verdict in verdict_counts:
                verdict_counts[verdict] += 1
        latest_round_status = str(latest_round.get("status") or "") if isinstance(latest_round, dict) else ""
        can_start_new_round = bool(ready) and (
            latest_round is None
            or latest_round_status in {"ready_for_rerun", "failed"}
            or (
                latest_round_status == "passed"
                and bool(latest_round.get("completed_at"))
                and objective_review_state.get("review_id") != str(latest_round.get("review_id") or "")
            )
        )
        override_active = bool(latest_override)
        review_clear = ready and bool(latest_round) and (
            override_active or (verdict_counts["concern"] == 0 and verdict_counts["remediation_required"] == 0)
        )
        phase = "promotion_review_pending" if ready and not latest_round else "promotion_review_active" if latest_round else "execution"
        if counts["active"] > 0 or counts["pending"] > 0:
            next_action = "Review findings were turned into remediation tasks. Continue in Atomic while the harness works through them."
            phase = "execution"
        elif unresolved_failed_count:
            next_action = "Resolve or disposition the remaining failed tasks before promotion can proceed."
            phase = "remediation_required"
        elif override_active:
            next_action = "The latest promotion review round was operator-approved. The objective is clear to promote."
            phase = "promotion_review_active"
        elif verdict_counts["remediation_required"] > 0 or verdict_counts["concern"] > 0:
            concern_total = verdict_counts["remediation_required"] + verdict_counts["concern"]
            if latest_round_status == "ready_for_rerun":
                next_action = f"Remediation from promotion review round {latest_round.get('round_number')} is complete. The harness should start the next review round now."
                phase = "promotion_review_pending"
            else:
                next_action = f"Promotion review found {concern_total} issue(s). Route remediation back into Atomic before promoting."
                phase = "remediation_required"
        elif latest_round_status == "running":
            next_action = f"Promotion review round {latest_round.get('round_number')} is running. Reviewer packets will appear as each agent finishes."
            phase = "promotion_review_active"
        elif latest_round:
            next_action = "Review the latest promotion packets and LLM affirmation details, then decide whether to promote the objective."
        else:
            next_action = "Execution is complete and no blockers remain. Automatic promotion review should begin next."
        recommended_view = "promotion-review" if ready and phase != "execution" else "atomic"
        return {
            "ready": ready,
            "review_clear": review_clear,
            "phase": phase,
            "recommended_view": recommended_view,
            "objective_review_state": objective_review_state,
            "verdict_counts": verdict_counts,
            "task_counts": counts,
            "waived_failed_count": waived_failed_count,
            "historical_failed_count": historical_failed_count,
            "unresolved_failed_count": unresolved_failed_count,
            "review_packet_count": len(all_review_packets),
            "objective_review_packet_count": sum(int((round_row.get("packet_count") or 0)) for round_row in review_rounds),
            "review_rounds": review_rounds,
            "can_start_new_round": can_start_new_round,
            "can_force_promote": bool(latest_round) and not override_active and counts["active"] == 0 and counts["pending"] == 0,
            "operator_override": latest_override,
            "review_packets": all_review_packets,
            "failed_tasks": failed_entries,
            "next_action": next_action,
        }
