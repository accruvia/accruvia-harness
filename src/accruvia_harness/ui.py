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




    def _truncate_text(self, text: str, limit: int) -> str:
        cleaned = " ".join((text or "").split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)].rstrip() + "..."



# Re-export from ui_routes for backward compatibility
from .ui_routes import (
    _EventBus,
    _build_fastapi_app,
    start_ui_server,
    _auto_start_supervisors,
    _resolve_ui_port,
)
