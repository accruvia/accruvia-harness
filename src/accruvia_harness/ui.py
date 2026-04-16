from __future__ import annotations

import json
import errno
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import multiprocessing as mp
import datetime as _dt
from dataclasses import asdict, dataclass, is_dataclass
import asyncio
from http import HTTPStatus
from pathlib import Path
from typing import Any
from enum import Enum

from queue import Queue, Empty
from fastapi import Request

from .commands.common import clear_ui_runtime_state, resolve_project_ref, update_ui_runtime_state

def _get_git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"

_GIT_COMMIT = _get_git_commit()
_SERVER_STARTED_AT = _dt.datetime.now(_dt.timezone.utc).isoformat()
from .context_control import objective_execution_gate
from .llm import LLMExecutionError, LLMInvocation
from .services.task_service import TaskService
from .services.workflow_timing_service import WorkflowTimingService
from .services.workflow_service import WorkflowService
from .context_recorder import ContextRecorder
from .frustration_triage import triage_frustration

# ui_responder and ui_memory deleted — mediation replaced by MCP server.
# Stubs for types still referenced in method signatures below.

def _mermaid_node_id_for_task(task_id: str) -> str:
    """Stable mermaid node id derived from a task id.

    The node id is the value written to tasks.mermaid_node_id and to the
    mermaid artifact content, so the two can be joined to answer "which
    task owns this node in the decomposition diagram."
    """
    suffix = task_id.split("_", 1)[-1][:12] if "_" in task_id else task_id[:12]
    return f"T_{suffix}"
# AttrDict supports both dict["key"] and dict.key access so auto-generated
# code that assumes dataclass-style attribute access works alongside existing
# dict-consuming code.
class _AttrDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)
    def __setattr__(self, key, value):
        self[key] = value

ConversationTurn = _AttrDict  # type: ignore[assignment,misc]
ResponderResult = _AttrDict  # type: ignore[assignment,misc]
ResponderContextPacket = _AttrDict  # type: ignore[assignment,misc]
from .domain import (
    ContextRecord,
    IntentModel,
    MermaidArtifact,
    MermaidStatus,
    Objective,
    ObjectivePhase,
    ObjectiveStatus,
    PromotionMode,
    PromotionStatus,
    RepoProvider,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    new_id,
    serialize_dataclass,
)


from .ui_coordinators import (
    AtomicGenerationCoordinator,
    BackgroundSupervisorCoordinator,
    ObjectiveReviewCoordinator,
)
from .ui_mixins._shared import (
    _ATOMIC_GENERATION,
    _OBJECTIVE_REVIEW,
    _BACKGROUND_SUPERVISOR,
    _MERMAID_RED_TEAM_MAX_ROUNDS,
    _INTERROGATION_RED_TEAM_MAX_ROUNDS,
    _ATOMIC_DECOMP_RED_TEAM_MAX_ROUNDS,
    _OBJECTIVE_REVIEW_DIMENSIONS,
    _OBJECTIVE_REVIEW_VERDICTS,
    _OBJECTIVE_REVIEW_PROGRESS,
    _OBJECTIVE_REVIEW_SEVERITIES,
    _OBJECTIVE_REVIEW_REBUTTAL_OUTCOMES,
    _TASK_REPLY_STALE_SECONDS,
    _OBJECTIVE_REVIEW_VAGUE_PHRASES,
)


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _run_task_question_job(
    *,
    db_path: str,
    workspace_root: str,
    log_path: str | None,
    config_file: str | None,
    project_id: str,
    objective_id: str | None,
    task_id: str,
    comment_record_id: str,
    comment_text: str,
    frustration_detected: bool,
    job_id: str,
    queued_at_iso: str,
) -> None:
    from .commands.common import build_context
    from .config import HarnessConfig
    from .store import SQLiteHarnessStore

    queued_at = _dt.datetime.fromisoformat(queued_at_iso)
    started_at = _dt.datetime.now(_dt.timezone.utc)
    monotonic_started = time.monotonic()
    worker_store = None
    try:
        worker_config = HarnessConfig.from_env(db_path, workspace_root, log_path, config_file)
        worker_ctx = build_context(worker_config)
        worker_store = worker_ctx.store
        worker_service = HarnessUIDataService(worker_ctx)
        responder_result = worker_service._answer_operator_comment(
            project_id=project_id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=comment_text,
            frustration_detected=frustration_detected,
        )
        completed_at = _dt.datetime.now(_dt.timezone.utc)
        elapsed_ms = int((time.monotonic() - monotonic_started) * 1000)
        queue_wait_ms = max(0, int((started_at - queued_at).total_seconds() * 1000))
        worker_service._log_ui_memory_retrieval(
            project_id=project_id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=comment_text,
            responder_result=responder_result,
        )
        worker_store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="harness_reply",
                project_id=project_id,
                objective_id=objective_id,
                task_id=task_id,
                visibility="operator_visible",
                author_type="system",
                content=responder_result.reply,
                metadata={
                    "reply_to": comment_record_id,
                    "status": "completed",
                    "job_id": job_id,
                    "queued_at": queued_at.isoformat(),
                    "started_at": started_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                    "elapsed_ms": elapsed_ms,
                    "queue_wait_ms": queue_wait_ms,
                    "recommended_action": responder_result.recommended_action,
                    "evidence_refs": responder_result.evidence_refs,
                    "mode_shift": responder_result.mode_shift,
                    "retrieved_memories": [serialize_dataclass(memory) for memory in responder_result.retrieved_memories],
                    "llm_backend": responder_result.llm_backend,
                    "prompt_path": responder_result.prompt_path,
                    "response_path": responder_result.response_path,
                },
            )
        )
    except Exception as exc:
        failed_at = _dt.datetime.now(_dt.timezone.utc)
        elapsed_ms = int((time.monotonic() - monotonic_started) * 1000)
        queue_wait_ms = max(0, int((started_at - queued_at).total_seconds() * 1000))
        try:
            if worker_store is None:
                worker_store = SQLiteHarnessStore(db_path)
            worker_store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="harness_reply_failed",
                    project_id=project_id,
                    objective_id=objective_id,
                    task_id=task_id,
                    visibility="operator_visible",
                    author_type="system",
                    content=str(exc),
                    metadata={
                        "reply_to": comment_record_id,
                        "status": "failed",
                        "job_id": job_id,
                        "queued_at": queued_at.isoformat(),
                        "started_at": started_at.isoformat(),
                        "completed_at": failed_at.isoformat(),
                        "elapsed_ms": elapsed_ms,
                        "queue_wait_ms": queue_wait_ms,
                    },
                )
            )
        except Exception:
            pass

@dataclass(slots=True)
class RunOutputSection:
    label: str
    path: str
    content: str


from .ui_mixins import (
    AtomicGenerationMixin,
    PromotionMixin,
    MermaidMixin,
    ObjectiveReviewMixin,
    InterrogationMixin,
    TaskAnalysisMixin,
    TaskExecutionMixin,
    ResponderMixin,
    SupervisorMixin,
    OperatorMixin,
    WorkspaceMixin,
)


class HarnessUIDataService(
    WorkspaceMixin,
    OperatorMixin,
    SupervisorMixin,
    ResponderMixin,
    TaskExecutionMixin,
    TaskAnalysisMixin,
    InterrogationMixin,
    ObjectiveReviewMixin,
    MermaidMixin,
    PromotionMixin,
    AtomicGenerationMixin,
):
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.store = ctx.store
        self.query_service = ctx.query_service
        self.workspace_root = ctx.config.workspace_root
        self.task_service = TaskService(self.store)
        self.workflow_service = WorkflowService(self.store)
        self.workflow_timing = WorkflowTimingService()
        self.context_recorder = ContextRecorder(self.store)
        self.memory_provider = None  # Mediation layer removed; MCP server replaces it
        self.auto_resume_atomic_generation = not bool(getattr(ctx, "is_test", False))
        self.auto_resume_objective_review = not bool(getattr(ctx, "is_test", False))
        self.background_workflow_enabled = not bool(getattr(ctx, "is_test", False))
        self.progress_callback = None
        self._harness_overview_cache_lock = threading.Lock()
        self._harness_overview_cache: tuple[float, dict[str, object]] | None = None

    def _workflow_async_mode(self) -> bool:
        return bool(self.background_workflow_enabled)

    def _emit_workflow_progress(self, event: dict[str, object]) -> None:
        callback = self.progress_callback
        if callback is not None:
            callback(dict(event))


    def invalidate_harness_overview_cache(self) -> None:
        with self._harness_overview_cache_lock:
            self._harness_overview_cache = None

    def reconcile_objective_workflow(self, objective_id: str) -> dict[str, object]:
        atomic_state = self._atomic_generation_state(objective_id)
        atomic_running = str(atomic_state.get("status") or "") == "running" and not bool(atomic_state.get("is_stale"))
        review_summary = self._promotion_review_for_objective(
            objective_id,
            [task for task in self.store.list_tasks(self.store.get_objective(objective_id).project_id) if task.objective_id == objective_id]
            if self.store.get_objective(objective_id) is not None
            else [],
        )
        review_state = self._objective_review_state(objective_id)
        return self.workflow_service.reconcile_objective(
            objective_id,
            start_atomic=(
                (lambda oid: self.queue_atomic_generation(oid, async_mode=self._workflow_async_mode()))
                if self.auto_resume_atomic_generation
                else None
            ),
            start_review=(
                (lambda oid: self.queue_objective_review(oid, async_mode=self._workflow_async_mode()))
                if self.auto_resume_objective_review
                else None
            ),
            atomic_running=atomic_running,
            review_running=str(review_state.get("status") or "") == "running",
            review_start_allowed=bool(review_summary.get("can_start_new_round", False)),
        )

    def reconcile_task_workflow(self, task: Task) -> None:
        if task.objective_id:
            self.reconcile_objective_workflow(task.objective_id)


    def create_objective(self, project_ref: str, title: str, summary: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ValueError("Objective title must not be empty")
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title=cleaned_title,
            summary=summary.strip(),
        )
        self.store.create_objective(objective)
        self._create_seed_mermaid(objective)
        self.reconcile_objective_workflow(objective.id)
        return {"objective": serialize_dataclass(objective)}

    def start_objective_lifecycle(self, objective_id: str) -> "ObjectiveLifecycleRunner":
        """Create an ObjectiveLifecycleRunner for the given objective.

        This is the contract-enforced path. The runner drives the
        objective through interrogation → mermaid_review → TRIO →
        executing → reviewing → promoted in strict order. Phase
        transitions are validated by advance_objective_phase — no
        phase can be skipped.

        For Temporal-backed deployments, use ObjectiveLifecycleWorkflow
        directly (started via the Temporal client). This method is for
        local/inline execution.
        """
        from .workflows.objective_lifecycle import ObjectiveLifecycleRunner
        config = getattr(self.ctx, "config", None)
        if config is None:
            raise ValueError("No config available on context")
        return ObjectiveLifecycleRunner(
            config=config.to_json(),
            objective_id=objective_id,
            store=self.store,
        )

    def update_intent_model(
        self,
        objective_id: str,
        *,
        intent_summary: str,
        success_definition: str,
        non_negotiables: list[str],
        frustration_signals: list[str],
        author_type: str = "operator",
    ) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        summary = intent_summary.strip()
        if not summary:
            raise ValueError("Intent summary must not be empty")
        model = IntentModel(
            id=new_id("intent"),
            objective_id=objective.id,
            version=self.store.next_intent_model_version(objective.id),
            intent_summary=summary,
            success_definition=success_definition.strip(),
            non_negotiables=[item for item in (part.strip() for part in non_negotiables) if item],
            frustration_signals=[item for item in (part.strip() for part in frustration_signals) if item],
            author_type=author_type,
        )
        self.store.create_intent_model(model)
        self.reconcile_objective_workflow(objective.id)
        return {"intent_model": serialize_dataclass(model)}


    def _skill_registry(self):
        registry = getattr(self.ctx, "skill_registry", None)
        if registry is None:
            engine = getattr(self.ctx, "engine", None)
            registry = getattr(engine, "skill_registry", None)
        if registry is None:
            from .skills import build_default_registry as _build
            registry = _build()
            try:
                setattr(self.ctx, "skill_registry", registry)
            except Exception:
                pass
        return registry

    def _red_team_loop_orchestrator(self, llm_router):
        """Build a RedTeamLoopOrchestrator wired to this request's context."""
        from .services.red_team_loop import RedTeamLoopOrchestrator
        return RedTeamLoopOrchestrator(
            skill_registry=self._skill_registry(),
            llm_router=llm_router,
            store=self.store,
            workspace_root=self.workspace_root,
            telemetry=getattr(self.ctx, "telemetry", None),
        )


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


    def _truncate_text(self, text: str, limit: int) -> str:
        cleaned = " ".join((text or "").split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)].rstrip() + "..."

    def _next_action_for_context(self, objective_id: str | None) -> dict[str, str]:
        if objective_id is None:
            return {
                "title": "Create or select an objective",
                "body": "Choose one objective to continue.",
            }
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return {
                "title": "Objective missing",
                "body": "The selected objective no longer exists.",
            }
        if not self.store.latest_intent_model(objective.id):
            return {
                "title": "Answer the desired outcome",
                "body": "Describe the result you want from this objective.",
            }
        review = self._interrogation_review(objective.id)
        if not review.get("completed"):
            return {
                "title": "Answer the next red-team question",
                "body": "The harness is interrogating and red-teaming the plan in the transcript before Mermaid review.",
            }
        latest_mermaid = self.store.latest_mermaid_artifact(objective.id, "workflow_control")
        if latest_mermaid is None or latest_mermaid.status != MermaidStatus.FINISHED:
            return {
                "title": "Finish or pause Mermaid review",
                "body": "Execution stays blocked until the current Mermaid is finished.",
            }
        gate = objective_execution_gate(self.store, objective.id)
        if not gate.ready:
            blocked = [check for check in gate.gate_checks if not check["ok"]]
            if blocked:
                return {
                    "title": str(blocked[0]["label"]),
                    "body": str(blocked[0].get("detail") or "That gate is still blocking execution."),
                }
        task, run = self._latest_linked_task_and_run(project_id=objective.project_id, objective_id=objective.id)
        if task is None:
            return {
                "title": "Create the first bounded slice",
                "body": "The harness should create the first bounded implementation step from the approved intent and Mermaid.",
            }
        if run is None:
            return {
                "title": "Ready to run the first implementation step",
                "body": "Start the current implementation step when you are ready.",
            }
        return {
            "title": "Review the latest attempt",
            "body": "Review the latest run evidence before deciding whether to continue, revise, or investigate.",
        }

    def _focus_mode_for_objective(self, objective_id: str) -> str:
        intent_model = self.store.latest_intent_model(objective_id)
        if intent_model is None or not (intent_model.intent_summary or "").strip():
            return "desired_outcome"
        if not (intent_model.success_definition or "").strip():
            return "success_definition"
        if not list(intent_model.non_negotiables):
            return "non_negotiables"
        review = self._interrogation_review(objective_id)
        if not review.get("completed"):
            return "interrogation_review"
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        if mermaid is None or mermaid.status != MermaidStatus.FINISHED:
            return "mermaid_review"
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return "empty"
        task, run = self._latest_linked_task_and_run(project_id=objective.project_id, objective_id=objective.id)
        if task is None or run is None:
            return "run_start"
        return "run_review"


# Re-export from ui_routes for backward compatibility
from .ui_routes import (
    _EventBus,
    _build_fastapi_app,
    start_ui_server,
    _auto_start_supervisors,
    _resolve_ui_port,
)
