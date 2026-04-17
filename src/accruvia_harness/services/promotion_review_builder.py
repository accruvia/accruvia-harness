"""Builds the promotion review status for an objective."""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from ..domain import (
    ContextRecord, Objective, ObjectiveStatus,
    PromotionStatus, Run, RunStatus, Task, TaskStatus,
    new_id, serialize_dataclass,
)
from ..ui_mixins._shared import (
    _OBJECTIVE_REVIEW,
    _OBJECTIVE_REVIEW_DIMENSIONS,
    _OBJECTIVE_REVIEW_VERDICTS,
)


class PromotionReviewBuilder:
    """Builds the comprehensive promotion review status dict for an objective."""

    def __init__(self, store: Any, *, workflow_timing: Any = None) -> None:
        self.store = store
        self.workflow_timing = workflow_timing

    def _objective_review_state(self, objective_id: str) -> dict[str, object]:
        from .review_state_service import ReviewStateService
        svc = ReviewStateService(self.store, workflow_timing=self.workflow_timing)
        return svc._objective_review_state(objective_id)

    def _normalize_objective_review_usage_metadata(self, metadata: dict) -> tuple:
        from .review_state_service import ReviewStateService
        svc = ReviewStateService(self.store)
        return svc._normalize_objective_review_usage_metadata(metadata)

    def _objective_review_evidence_contract(self, packet: dict) -> dict:
        from .remediation_service import RemediationService
        svc = RemediationService(self.store, None)
        return svc.extract_evidence_contract(packet)

    def build(
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
