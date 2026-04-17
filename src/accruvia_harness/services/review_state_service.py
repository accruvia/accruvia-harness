"""Objective review state queries: current state, staleness detection, usage tracking."""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from ..domain import ContextRecord, Objective, ObjectiveStatus, new_id
from ..ui_mixins._shared import _OBJECTIVE_REVIEW


class ReviewStateService:
    """Extracted from ObjectiveReviewMixin."""

    def __init__(self, store: Any, *, workflow_timing: Any = None, ctx: Any = None, emit_progress: Any = None) -> None:
        self.store = store
        self.workflow_timing = workflow_timing
        self.ctx = ctx
        self._emit_workflow_progress = emit_progress or (lambda x: None)

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

