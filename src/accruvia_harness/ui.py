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


class AtomicGenerationCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def start(self, objective_id: str, worker) -> bool:
        with self._lock:
            if objective_id in self._running:
                return False
            self._running.add(objective_id)
        thread = threading.Thread(target=self._run, args=(objective_id, worker), daemon=True)
        thread.start()
        return True

    def _run(self, objective_id: str, worker) -> None:
        try:
            worker()
        finally:
            with self._lock:
                self._running.discard(objective_id)


_ATOMIC_GENERATION = AtomicGenerationCoordinator()


class ObjectiveReviewCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def start(self, objective_id: str, worker) -> bool:
        with self._lock:
            if objective_id in self._running:
                return False
            self._running.add(objective_id)
        thread = threading.Thread(target=self._run, args=(objective_id, worker), daemon=True)
        thread.start()
        return True

    def _run(self, objective_id: str, worker) -> None:
        try:
            worker()
        finally:
            with self._lock:
                self._running.discard(objective_id)


_OBJECTIVE_REVIEW = ObjectiveReviewCoordinator()
_MERMAID_RED_TEAM_MAX_ROUNDS = 20
_INTERROGATION_RED_TEAM_MAX_ROUNDS = 4
_ATOMIC_DECOMP_RED_TEAM_MAX_ROUNDS = 4

_OBJECTIVE_REVIEW_DIMENSIONS = frozenset(
    {
        "intent_fidelity",
        "unit_test_coverage",
        "integration_e2e_coverage",
        "security",
        "devops",
        "atomic_fidelity",
        "code_structure",
    }
)
_OBJECTIVE_REVIEW_VERDICTS = frozenset({"pass", "concern", "remediation_required"})
_OBJECTIVE_REVIEW_PROGRESS = frozenset(
    {"new_concern", "still_blocking", "improving", "resolved", "not_applicable"}
)
_OBJECTIVE_REVIEW_SEVERITIES = frozenset({"low", "medium", "high"})
_OBJECTIVE_REVIEW_REBUTTAL_OUTCOMES = frozenset(
    {"accepted", "wrong_artifact_type", "artifact_incomplete", "missing_terminal_event", "evidence_not_found"}
)
_TASK_REPLY_STALE_SECONDS = 90


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
_OBJECTIVE_REVIEW_VAGUE_PHRASES = (
    "improve",
    "better",
    "more coverage",
    "additional tests",
    "stronger evidence",
    "more evidence",
    "further validation",
    "review further",
    "be reviewed",
)


class BackgroundSupervisorCoordinator:
    """Manages background supervisor threads, one per project."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, threading.Event] = {}  # project_id -> stop event
        self._status: dict[str, dict[str, object]] = {}  # project_id -> latest status

    def start(self, project_id: str, engine, *, watch: bool = True) -> bool:
        with self._lock:
            if project_id in self._running:
                return False
            stop_event = threading.Event()
            self._running[project_id] = stop_event
            self._status[project_id] = {
                "state": "starting",
                "processed_count": 0,
                "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }

        def worker() -> None:
            try:
                # Wire stop signal to the worker so it kills the subprocess on stop.
                if hasattr(engine.worker, "set_stop_requested"):
                    engine.worker.set_stop_requested(stop_event.is_set)
                self._status[project_id]["state"] = "running"
                result = engine.supervise(
                    project_id=project_id,
                    worker_id=f"ui-supervisor-{project_id[:8]}",
                    watch=watch,
                    idle_sleep_seconds=10.0,
                    max_idle_cycles=None,
                    stop_requested=stop_event.is_set,
                    progress_callback=lambda ev: self._on_progress(project_id, ev),
                )
                self._status[project_id].update({
                    "state": "finished",
                    "processed_count": result.processed_count,
                    "exit_reason": result.exit_reason,
                    "finished_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                })
            except Exception as exc:
                self._status[project_id].update({
                    "state": "error",
                    "error": str(exc),
                    "finished_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                })
            finally:
                if hasattr(engine.worker, "set_stop_requested"):
                    engine.worker.set_stop_requested(None)
                with self._lock:
                    self._running.pop(project_id, None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return True

    def stop(self, project_id: str) -> bool:
        with self._lock:
            stop_event = self._running.get(project_id)
            if stop_event is None:
                return False
            stop_event.set()
            status = self._status.get(project_id, {})
            status["state"] = "stopping"
            return True

    def is_running(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._running

    def status(self, project_id: str) -> dict[str, object]:
        return dict(self._status.get(project_id, {"state": "idle"}))

    def _on_progress(self, project_id: str, event: dict[str, object]) -> None:
        event_type = event.get("type", "")
        status = self._status.get(project_id, {})
        if event_type == "task_finished":
            status["processed_count"] = status.get("processed_count", 0) + 1
            status["last_task_id"] = event.get("task_id")
            status["last_task_title"] = event.get("task_title")
            status["last_task_status"] = event.get("status")
        status["last_event"] = event_type
        status["last_event_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()


_BACKGROUND_SUPERVISOR = BackgroundSupervisorCoordinator()


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


class HarnessUIDataService:
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

    def _supervisor_control_dir(self) -> Path:
        return self.ctx.config.db_path.parent / "supervisors"

    def _live_supervisor_records(self, project_id: str) -> list[dict[str, object]]:
        control_dir = self._supervisor_control_dir()
        if not control_dir.exists():
            return []
        live_records: list[dict[str, object]] = []
        for path in sorted(control_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            pid = int(payload.get("pid") or 0)
            if pid <= 0:
                continue
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            record_project_id = str(payload.get("project_id") or "").strip()
            if record_project_id and record_project_id != project_id:
                continue
            live_records.append(payload)
        return live_records

    def list_projects(self) -> dict[str, object]:
        projects = []
        for project in self.store.list_projects():
            metrics = self.store.metrics_snapshot(project.id)
            projects.append(
                {
                    **serialize_dataclass(project),
                    "queue_depth": int(metrics.get("tasks_by_status", {}).get("pending", 0))
                    + int(metrics.get("tasks_by_status", {}).get("active", 0)),
                }
            )
        return {"projects": projects}

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

    def _workflow_status_for_objective(
        self,
        objective: Objective,
        linked_tasks: list[Task],
        promotion_review: dict[str, object],
        repo_promotion: dict[str, object],
    ) -> dict[str, object]:
        planning = self.workflow_service.planning_readiness(objective.id)
        execution = self.workflow_service.execution_readiness(objective.id, linked_tasks)
        review = self.workflow_service.review_readiness(objective.id, linked_tasks)
        promotion_checks = [
            {
                "key": "review_clear",
                "label": "Objective review clear",
                "ok": bool(promotion_review.get("review_clear")),
                "detail": "" if bool(promotion_review.get("review_clear")) else str(promotion_review.get("next_action") or "Objective review is not clear yet."),
            },
            {
                "key": "repo_promotion_eligible",
                "label": "Repo promotion eligible",
                "ok": bool(repo_promotion.get("eligible")),
                "detail": "" if bool(repo_promotion.get("eligible")) else str(repo_promotion.get("reason") or "Repo promotion is not eligible yet."),
            },
        ]
        promotion = {
            "stage": "promotion",
            "ready": all(bool(check["ok"]) for check in promotion_checks),
            "checks": promotion_checks,
        }
        current_stage = (
            "promotion"
            if objective.status == ObjectiveStatus.RESOLVED and bool(promotion_review.get("review_rounds"))
            else "review"
            if objective.status == ObjectiveStatus.RESOLVED
            else "execution"
            if objective.status == ObjectiveStatus.EXECUTING
            else "planning"
        )
        return {
            "current_stage": current_stage,
            "planning": {"ready": planning.ready, "checks": _to_jsonable(planning.checks)},
            "execution": {"ready": execution.ready, "checks": _to_jsonable(execution.checks)},
            "review": {"ready": review.ready, "checks": _to_jsonable(review.checks)},
            "promotion": promotion,
        }

    def update_project_repo_settings(
        self,
        project_id: str,
        *,
        promotion_mode: str,
        repo_provider: str,
        repo_name: str,
        base_branch: str,
    ) -> dict[str, object]:
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        cleaned_repo_name = repo_name.strip()
        cleaned_base_branch = base_branch.strip()
        if not cleaned_repo_name:
            raise ValueError("Repository name must not be empty")
        if not cleaned_base_branch:
            raise ValueError("Base branch must not be empty")
        updated = self.task_service.update_project(
            project.id,
            promotion_mode=PromotionMode(promotion_mode),
            repo_provider=RepoProvider(repo_provider),
            repo_name=cleaned_repo_name,
            base_branch=cleaned_base_branch,
        )
        return {"project": serialize_dataclass(updated)}

    def promote_objective_to_repo(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        project = self.store.get_project(objective.project_id)
        if project is None:
            raise ValueError(f"Unknown project for objective: {objective.project_id}")
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
        review = self._promotion_review_for_objective(objective.id, linked_tasks)
        override_active = bool(review.get("operator_override"))
        if not bool(review.get("review_clear")) and not override_active:
            raise ValueError("Objective is not yet clear to promote")
        candidate_tasks = self._completed_unapplied_tasks_for_objective(linked_tasks)
        if not candidate_tasks:
            raise ValueError("No unapplied completed atomic units are available for repo promotion")
        return self._apply_repo_promotion_for_tasks(objective, project, linked_tasks, candidate_tasks)

    def promote_atomic_unit_to_repo(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        objective_id = str(task.objective_id or "").strip()
        if not objective_id:
            raise ValueError("Atomic-unit repo promotion requires a task linked to an objective")
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective for task: {objective_id}")
        project = self.store.get_project(objective.project_id)
        if project is None:
            raise ValueError(f"Unknown project for objective: {objective.project_id}")
        linked_tasks = [candidate for candidate in self.store.list_tasks(objective.project_id) if candidate.objective_id == objective.id]
        if task.status != TaskStatus.COMPLETED:
            raise ValueError("Only completed atomic units can be promoted to the repository")
        if self._task_repo_applied(task):
            raise ValueError("This atomic unit has already been promoted to the repository")
        return self._apply_repo_promotion_for_tasks(objective, project, linked_tasks, [task])

    def _apply_repo_promotion_for_tasks(
        self,
        objective: Objective,
        project: Project,
        linked_tasks: list[Task],
        candidate_tasks: list[Task],
    ) -> dict[str, object]:
        review = self._promotion_review_for_objective(objective.id, linked_tasks)
        override_active = bool(review.get("operator_override"))
        if not bool(review.get("review_clear")) and not override_active:
            raise ValueError("Objective is not yet clear to promote")
        blocker_reason = self._unapplied_repo_promotion_blocker(candidate_tasks)
        if blocker_reason:
            raise ValueError(blocker_reason)
        source_repo_root = self._objective_source_repo_root(objective.id, linked_tasks)
        if source_repo_root is None:
            raise ValueError("Objective promotion requires a git-backed source repository root")
        objective_paths = self._objective_repo_file_set(candidate_tasks)
        if not objective_paths:
            raise ValueError("Objective promotion could not determine any objective-related file paths to apply")
        candidate = candidate_tasks[-1]
        candidate_run = self._latest_completed_run(candidate)
        candidate_run_id = candidate_run.id if candidate_run is not None else ""
        apply_result = self.ctx.engine.repository_promotions.apply_objective(
            project,
            objective_id=objective.id,
            objective_title=objective.title,
            source_repo_root=source_repo_root,
            source_working_root=source_repo_root,
            objective_paths=objective_paths,
            staging_root=self.workspace_root / "objective_promotions",
        )
        applyback = {
            "status": "applied",
            "branch_name": apply_result.branch_name,
            "commit_sha": apply_result.commit_sha,
            "pushed_ref": apply_result.pushed_ref,
            "pr_url": apply_result.pr_url,
            "promotion_mode": project.promotion_mode.value,
            "cleanup_performed": apply_result.cleanup_performed,
            "verified_remote_sha": apply_result.verified_remote_sha,
            "objective_paths": objective_paths,
            "applied_task_ids": [task.id for task in candidate_tasks],
            "applied_task_count": len(candidate_tasks),
            "source_repo_root": str(source_repo_root),
        }
        applied_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        for task in candidate_tasks:
            metadata = dict(task.external_ref_metadata) if isinstance(task.external_ref_metadata, dict) else {}
            task_run = self._latest_completed_run(task)
            metadata["repo_applyback"] = {
                "applied_commit_sha": apply_result.commit_sha,
                "applied_at": applied_at,
                "pushed_ref": apply_result.pushed_ref,
                "objective_id": objective.id,
                "run_id": task_run.id if task_run is not None else "",
            }
            self.store.update_task_external_metadata(task.id, metadata)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                task_id=candidate.id if candidate is not None else None,
                run_id=candidate_run_id or None,
                visibility="operator_visible",
                author_type="system",
                content="Action receipt: Promoted the objective snapshot to the repository.",
                metadata={
                    "kind": "objective_repo_promotion",
                    "task_id": candidate.id if candidate is not None else "",
                    "run_id": candidate_run_id,
                    "promotion_status": "approved",
                    "applyback": applyback,
                    "objective_paths": objective_paths,
                    "applied_task_ids": [task.id for task in candidate_tasks],
                },
            )
        )
        return {
            "objective_id": objective.id,
            "task_id": candidate.id if candidate is not None else "",
            "run_id": candidate_run_id,
            "promotion": {
                "id": new_id("promotion"),
                "task_id": candidate.id if candidate is not None else "",
                "run_id": candidate_run_id,
                "status": "approved",
                "summary": "Objective snapshot promoted to the repository.",
                "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
            },
            "applyback": applyback,
        }

    def project_workspace(self, project_ref: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objectives = self.store.list_objectives(project.id)
        for objective in objectives:
            self.reconcile_objective_workflow(objective.id)
        if self.auto_resume_atomic_generation:
            for objective in objectives:
                self._maybe_resume_atomic_generation(objective.id)
        if self.auto_resume_objective_review:
            for objective in objectives:
                self._maybe_resume_objective_review(objective.id)
        objectives = self.store.list_objectives(project.id)
        tasks = self.store.list_tasks(project.id)
        objective_task_map = {objective.id: [task for task in tasks if task.objective_id == objective.id] for objective in objectives}
        review_map: dict[str, dict[str, object]] = {}
        repo_promotion_map: dict[str, dict[str, object]] = {}
        workflow_map: dict[str, dict[str, object]] = {}
        for objective in objectives:
            linked_tasks = objective_task_map.get(objective.id, [])
            review_map[objective.id] = self._promotion_review_for_objective(objective.id, linked_tasks)
            repo_promotion_map[objective.id] = self._repo_promotion_for_objective(objective.id, linked_tasks)
            workflow_map[objective.id] = self._workflow_status_for_objective(
                objective,
                linked_tasks,
                review_map[objective.id],
                repo_promotion_map[objective.id],
            )
        task_payload = []
        latest_runs_by_task: dict[str, list[Any]] = {}
        for task in tasks:
            runs = self.store.list_runs(task.id)
            promotions = self.store.list_promotions(task.id)
            latest_runs_by_task[task.id] = runs
            review_ready = False
            if task.objective_id:
                review_ready = bool((workflow_map.get(task.objective_id) or {}).get("review", {}).get("ready"))
            task_payload.append(
                {
                    **serialize_dataclass(task),
                    "runs": [serialize_dataclass(run) for run in runs],
                    "promotions": [serialize_dataclass(promotion) for promotion in promotions],
                    "queue_state": self.workflow_service.queue_state_for_task(task, review_ready=review_ready),
                }
            )
        objective_payload = []
        for objective in objectives:
            latest_intent = self.store.latest_intent_model(objective.id)
            latest_mermaid = self.store.latest_mermaid_artifact(objective.id)
            latest_proposal = self._latest_mermaid_proposal(objective.id)
            gate = objective_execution_gate(self.store, objective.id)
            linked_tasks = objective_task_map.get(objective.id, [])
            atomic_generation = self._atomic_generation_state(objective.id)
            promotion_review = review_map[objective.id]
            repo_promotion = repo_promotion_map[objective.id]
            workflow = workflow_map[objective.id]
            objective_payload.append(
                {
                    **serialize_dataclass(objective),
                    "execution_gate": {
                        "ready": gate.ready,
                        "checks": _to_jsonable(gate.gate_checks),
                    },
                    "workflow": workflow,
                    "intent_model": serialize_dataclass(latest_intent) if latest_intent is not None else None,
                    "interrogation_review": self._interrogation_review(objective.id),
                    "diagram": (
                        {
                            **serialize_dataclass(latest_mermaid),
                            "content": latest_mermaid.content,
                        }
                        if latest_mermaid is not None
                        else None
                    ),
                    "diagram_proposal": latest_proposal,
                    "linked_task_count": len(linked_tasks),
                    "atomic_generation": atomic_generation,
                    "atomic_units": self._atomic_units_for_objective(objective.id, linked_tasks, atomic_generation),
                    "promotion_review": promotion_review,
                    "repo_promotion": repo_promotion,
                    "recommended_view": (
                        "promotion-review"
                        if workflow.get("review", {}).get("ready") or objective.status == ObjectiveStatus.RESOLVED
                        else "atomic"
                    ),
                    "proposed_first_task": self.proposed_first_task(objective.id)
                    if gate.ready and not linked_tasks
                    else None,
                }
            )
        return {
            "project": serialize_dataclass(project),
            "objectives": objective_payload,
            "tasks": task_payload,
            "comments": self._operator_comments(project.id),
            "replies": self._harness_replies(project.id),
            "action_receipts": self._action_receipts(project.id),
            "frustrations": self._operator_frustrations(project.id),
            "loop_status": self.query_service.project_summary(project.id)["loop_status"],
            "diagram": {
                "label": "Project control flow",
                "mermaid": self._project_mermaid(project.id, tasks, latest_runs_by_task),
            },
            "supervisor": {
                "running": _BACKGROUND_SUPERVISOR.is_running(project.id),
                **_BACKGROUND_SUPERVISOR.status(project.id),
            },
        }

    def project_summary_fast(self, project_ref: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objectives = self.store.list_objectives(project.id)
        tasks = self.store.list_tasks(project.id)
        task_counts_by_objective: dict[str, dict[str, int]] = {
            objective.id: {"completed": 0, "active": 0, "failed": 0, "pending": 0}
            for objective in objectives
        }
        for task in tasks:
            if not task.objective_id or task.objective_id not in task_counts_by_objective:
                continue
            status = task.status.value if hasattr(task.status, "value") else str(task.status)
            if status in task_counts_by_objective[task.objective_id]:
                task_counts_by_objective[task.objective_id][status] += 1
        objective_payload = [
            {
                "id": objective.id,
                "project_id": project.id,
                "title": objective.title,
                "status": objective.status.value,
                "task_counts": task_counts_by_objective.get(objective.id, {}),
                "task_total": sum(task_counts_by_objective.get(objective.id, {}).values()),
            }
            for objective in objectives
        ]
        objective_titles = {objective.id: objective.title for objective in objectives}
        task_payload = [
            {
                "id": task.id,
                "objective_id": task.objective_id,
                "objective_title": objective_titles.get(task.objective_id or "", ""),
                "title": task.title,
                "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "updated_at": task.updated_at.isoformat(),
            }
            for task in tasks
        ]
        return {
            "project": serialize_dataclass(project),
            "objectives": objective_payload,
            "tasks": task_payload,
            "supervisor": {
                "running": _BACKGROUND_SUPERVISOR.is_running(project.id),
                **_BACKGROUND_SUPERVISOR.status(project.id),
            },
        }

    def project_objectives_detail(self, project_ref: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objectives = self.store.list_objectives(project.id)
        tasks = self.store.list_tasks(project.id)
        objective_task_map = {objective.id: [task for task in tasks if task.objective_id == objective.id] for objective in objectives}
        payload = []
        for objective in objectives:
            linked_tasks = objective_task_map.get(objective.id, [])
            review = self._promotion_review_for_objective(objective.id, linked_tasks)
            workflow = self._harness_workflow_status_for_objective(objective, linked_tasks)
            gate = objective_execution_gate(self.store, objective.id)
            payload.append(
                {
                    "id": objective.id,
                    "project_id": project.id,
                    "title": objective.title,
                    "status": objective.status.value,
                    "execution_gate": {
                        "ready": gate.ready,
                        "checks": _to_jsonable(gate.gate_checks),
                    },
                    "workflow": workflow,
                    "promotion_review": {
                        "review_clear": bool(review.get("review_clear")),
                        "review_rounds": review.get("review_rounds") or [],
                    },
                }
            )
        return {
            "project": serialize_dataclass(project),
            "objectives": payload,
        }

    def project_objective_detail(self, project_ref: str, objective_id: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objective = self.store.get_objective(objective_id)
        if objective is None or objective.project_id != project.id:
            raise ValueError(f"Unknown objective for project: {objective_id}")
        current_interrogation = self._interrogation_review(objective.id)
        if not current_interrogation.get("completed") and self._should_auto_complete_interrogation(objective.id):
            self._persist_interrogation_record("interrogation_completed", objective, current_interrogation)
            self.reconcile_objective_workflow(objective.id)
        tasks = [task for task in self.store.list_tasks(project.id) if task.objective_id == objective.id]
        review = self._promotion_review_for_objective(objective.id, tasks)
        repo_promotion = self._repo_promotion_for_objective(objective.id, tasks)
        workflow = self._workflow_status_for_objective(objective, tasks, review, repo_promotion)
        gate = objective_execution_gate(self.store, objective.id)
        latest_intent = self.store.latest_intent_model(objective.id)
        latest_mermaid = self.store.latest_mermaid_artifact(objective.id)
        latest_proposal = self._latest_mermaid_proposal(objective.id)
        comment_records = self.store.list_context_records(objective_id=objective.id, record_type="operator_comment")[-12:]
        reply_records = self.store.list_context_records(objective_id=objective.id, record_type="harness_reply")[-12:]
        receipt_records = self.store.list_context_records(objective_id=objective.id, record_type="action_receipt")[-12:]
        task_payload = [
            {
                "id": task.id,
                "objective_id": task.objective_id,
                "title": task.title,
                "strategy": task.strategy,
                "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "updated_at": task.updated_at.isoformat(),
            }
            for task in tasks
        ]
        return {
            "project": serialize_dataclass(project),
            "objective": {
                **serialize_dataclass(objective),
                "execution_gate": {
                    "ready": gate.ready,
                    "checks": _to_jsonable(gate.gate_checks),
                },
                "workflow": workflow,
                "intent_model": serialize_dataclass(latest_intent) if latest_intent is not None else None,
                "interrogation_review": self._interrogation_review(objective.id),
                "diagram": (
                    {
                        **serialize_dataclass(latest_mermaid),
                        "content": latest_mermaid.content,
                    }
                    if latest_mermaid is not None
                    else None
                ),
                "diagram_proposal": latest_proposal,
                "promotion_review": review,
            },
            "tasks": task_payload,
            "comments": [
                {
                    "id": record.id,
                    "text": record.content,
                    "author": record.author_id,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id,
                    "task_id": record.task_id,
                }
                for record in comment_records
            ],
            "replies": [
                {
                    "id": record.id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id,
                    "task_id": record.task_id,
                    "reply_to": str(record.metadata.get("reply_to") or ""),
                }
                for record in reply_records
            ],
            "receipts": [
                {
                    "id": record.id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id,
                    "task_id": record.task_id,
                    "kind": str(record.metadata.get("kind") or ""),
                    "status": str(record.metadata.get("status") or ""),
                }
                for record in receipt_records
            ],
        }

    def project_token_performance(self, project_ref: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objectives = self.store.list_objectives(project.id)

        def summarize_packets(packet_list: list[dict[str, object]] | None) -> dict[str, float | int]:
            usage = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "latency_ms": 0,
                "reported_packet_count": 0,
                "unreported_packet_count": 0,
            }
            for packet in packet_list or []:
                llm_usage = packet.get("llm_usage") if isinstance(packet, dict) else {}
                llm_usage = llm_usage if isinstance(llm_usage, dict) else {}
                reported = packet.get("llm_usage_reported") is not False if isinstance(packet, dict) else True
                if reported:
                    usage["prompt_tokens"] += int(llm_usage.get("prompt_tokens") or 0)
                    usage["completion_tokens"] += int(llm_usage.get("completion_tokens") or 0)
                    usage["total_tokens"] += int(llm_usage.get("total_tokens") or 0)
                    usage["cost_usd"] += float(llm_usage.get("cost_usd") or 0.0)
                    usage["latency_ms"] += int(llm_usage.get("latency_ms") or 0)
                    usage["reported_packet_count"] += 1
                else:
                    usage["unreported_packet_count"] += 1
                    usage["latency_ms"] += int(llm_usage.get("latency_ms") or 0)
            return usage

        def add_usage(
            target: dict[str, float | int],
            usage: dict[str, float | int],
            *,
            packet_count: int = 0,
            round_count: int = 0,
        ) -> None:
            target["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            target["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            target["total_tokens"] += int(usage.get("total_tokens") or 0)
            target["cost_usd"] += float(usage.get("cost_usd") or 0.0)
            target["latency_ms"] += int(usage.get("latency_ms") or 0)
            target["packet_count"] += packet_count
            target["round_count"] += round_count
            target["reported_packet_count"] += int(usage.get("reported_packet_count") or 0)
            target["unreported_packet_count"] += int(usage.get("unreported_packet_count") or 0)

        totals: dict[str, float | int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": 0,
            "packet_count": 0,
            "round_count": 0,
            "reported_packet_count": 0,
            "unreported_packet_count": 0,
        }
        objective_rows: list[dict[str, object]] = []
        reviewer_rows: dict[str, dict[str, object]] = {}
        round_rows: list[dict[str, object]] = []

        for objective in objectives:
            linked_tasks = [task for task in self.store.list_tasks(project.id) if task.objective_id == objective.id]
            review = self._promotion_review_for_objective(objective.id, linked_tasks)
            rounds = list(review.get("review_rounds") or [])
            if not rounds:
                continue
            objective_usage: dict[str, float | int] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "latency_ms": 0,
                "packet_count": 0,
                "round_count": 0,
                "reported_packet_count": 0,
                "unreported_packet_count": 0,
            }
            for round_row in rounds:
                packets = list(round_row.get("packets") or [])
                round_usage = summarize_packets(packets)
                add_usage(objective_usage, round_usage, packet_count=len(packets), round_count=1)
                add_usage(totals, round_usage, packet_count=len(packets), round_count=1)
                round_rows.append(
                    {
                        "objective_id": objective.id,
                        "objective_title": objective.title,
                        "round_number": round_row.get("round_number"),
                        "status": round_row.get("status"),
                        "packet_count": len(packets),
                        "usage": round_usage,
                        "last_activity_at": round_row.get("last_activity_at"),
                    }
                )
                for packet in packets:
                    reviewer = str(packet.get("reviewer") or packet.get("dimension") or "unknown")
                    current = reviewer_rows.get(
                        reviewer,
                        {
                            "reviewer": reviewer,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                            "cost_usd": 0.0,
                            "latency_ms": 0,
                            "packet_count": 0,
                            "reported_packet_count": 0,
                            "unreported_packet_count": 0,
                        },
                    )
                    packet_usage = summarize_packets([packet])
                    current["prompt_tokens"] = int(current["prompt_tokens"]) + int(packet_usage["prompt_tokens"])
                    current["completion_tokens"] = int(current["completion_tokens"]) + int(packet_usage["completion_tokens"])
                    current["total_tokens"] = int(current["total_tokens"]) + int(packet_usage["total_tokens"])
                    current["cost_usd"] = float(current["cost_usd"]) + float(packet_usage["cost_usd"])
                    current["latency_ms"] = int(current["latency_ms"]) + int(packet_usage["latency_ms"])
                    current["packet_count"] = int(current["packet_count"]) + 1
                    current["reported_packet_count"] = int(current["reported_packet_count"]) + int(packet_usage["reported_packet_count"])
                    current["unreported_packet_count"] = int(current["unreported_packet_count"]) + int(packet_usage["unreported_packet_count"])
                    reviewer_rows[reviewer] = current
            objective_rows.append(
                {
                    "objective_id": objective.id,
                    "title": objective.title,
                    "round_count": int(objective_usage["round_count"]),
                    "packet_count": int(objective_usage["packet_count"]),
                    "usage": objective_usage,
                }
            )

        objective_rows.sort(key=lambda item: int((item.get("usage") or {}).get("total_tokens") or 0), reverse=True)
        round_rows.sort(key=lambda item: int((item.get("usage") or {}).get("total_tokens") or 0), reverse=True)
        reviewers = sorted(reviewer_rows.values(), key=lambda item: int(item.get("total_tokens") or 0), reverse=True)
        avg_tokens_per_round = int(int(totals["total_tokens"]) / int(totals["round_count"])) if int(totals["round_count"]) else 0
        avg_cost_per_round = float(totals["cost_usd"]) / int(totals["round_count"]) if int(totals["round_count"]) else 0.0
        avg_tokens_per_packet = int(int(totals["total_tokens"]) / int(totals["packet_count"])) if int(totals["packet_count"]) else 0

        return {
            "project": serialize_dataclass(project),
            "totals": totals,
            "summary": {
                "avg_tokens_per_round": avg_tokens_per_round,
                "avg_cost_per_round": avg_cost_per_round,
                "avg_tokens_per_packet": avg_tokens_per_packet,
            },
            "objectives": objective_rows,
            "reviewers": reviewers,
            "rounds": round_rows[:50],
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

    def _latest_completed_task_for_objective(self, linked_tasks: list[Task]) -> Task | None:
        best: tuple[str, str, str] | None = None
        selected: Task | None = None
        for task in linked_tasks:
            if task.status != TaskStatus.COMPLETED:
                continue
            runs = self.store.list_runs(task.id)
            completed_run = next((run for run in reversed(runs) if run.status == RunStatus.COMPLETED), None)
            if completed_run is None:
                continue
            score = (
                str(completed_run.created_at or ""),
                str(task.created_at or ""),
                task.id,
            )
            if best is None or score > best:
                best = score
                selected = task
        return selected

    def _objective_repo_file_set(self, linked_tasks: list[Task]) -> list[str]:
        file_paths: set[str] = set()
        for task in linked_tasks:
            runs = self.store.list_runs(task.id)
            for run in runs:
                report_artifacts = [artifact for artifact in self.store.list_artifacts(run.id) if artifact.kind == "report" and artifact.path]
                if not report_artifacts:
                    continue
                report_path = Path(report_artifacts[-1].path)
                try:
                    payload = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                changed_files = payload.get("changed_files")
                if isinstance(changed_files, list):
                    for raw_path in changed_files:
                        path = str(raw_path or "").strip()
                        if path and not path.startswith("/") and ".." not in Path(path).parts:
                            file_paths.add(str(Path(path)))
        return sorted(file_paths)

    def _latest_completed_run(self, task: Task) -> Run | None:
        runs = self.store.list_runs(task.id)
        return next((run for run in reversed(runs) if run.status == RunStatus.COMPLETED), None)

    def _task_repo_applied(self, task: Task) -> bool:
        metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
        repo_applyback = metadata.get("repo_applyback") if isinstance(metadata.get("repo_applyback"), dict) else {}
        return bool(str(repo_applyback.get("applied_commit_sha") or "").strip())

    def _completed_unapplied_tasks_for_objective(self, linked_tasks: list[Task]) -> list[Task]:
        ordered: list[tuple[tuple[str, str, str], Task]] = []
        for task in linked_tasks:
            if task.status != TaskStatus.COMPLETED:
                continue
            if self._task_repo_applied(task):
                continue
            completed_run = self._latest_completed_run(task)
            if completed_run is None:
                continue
            ordered.append(
                (
                    (
                        str(completed_run.created_at or ""),
                        str(task.created_at or ""),
                        task.id,
                    ),
                    task,
                )
            )
        ordered.sort(key=lambda item: item[0])
        return [task for _, task in ordered]

    def _unapplied_repo_promotion_blocker(self, tasks: list[Task]) -> str:
        for task in tasks:
            completed_run = self._latest_completed_run(task)
            if completed_run is None:
                return f"Completed atomic unit '{task.title}' does not have a completed run."
            missing_validation_reason = self._missing_repo_promotion_validation_reason(completed_run.id)
            if missing_validation_reason:
                return f"Completed atomic unit '{task.title}' is not ready for repo promotion. {missing_validation_reason}"
        return ""

    def _objective_source_repo_root(self, objective_id: str, linked_tasks: list[Task]) -> Path | None:
        for task in reversed(linked_tasks):
            runs = self.store.list_runs(task.id)
            for run in reversed(runs):
                events = self.store.list_events(entity_type="run", entity_id=run.id)
                for event in reversed(events):
                    if event.event_type != "project_workspace_prepared":
                        continue
                    source_repo_root = str(event.payload.get("source_repo_root") or "").strip()
                    if source_repo_root:
                        return Path(source_repo_root).resolve()
        return None

    def _latest_objective_repo_promotion(self, objective_id: str) -> dict[str, object] | None:
        records = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="action_receipt")
            if str(record.metadata.get("kind") or "") == "objective_repo_promotion"
        ]
        if not records:
            return None
        record = records[-1]
        applyback = dict(record.metadata.get("applyback") or {})
        return {
            "id": record.id,
            "status": "approved",
            "summary": record.content,
            "created_at": record.created_at.isoformat(),
            "applyback": applyback,
            "task_id": str(record.metadata.get("task_id") or ""),
            "run_id": str(record.metadata.get("run_id") or ""),
        }

    def _missing_repo_promotion_validation_reason(self, run_id: str) -> str:
        report_artifacts = [artifact for artifact in self.store.list_artifacts(run_id) if artifact.kind == "report" and artifact.path]
        if not report_artifacts:
            return "The latest completed run does not have a structured report artifact."
        report_path = Path(report_artifacts[-1].path)
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "The latest completed run has an unreadable structured report artifact."
        compile_check = payload.get("compile_check")
        test_check = payload.get("test_check")
        if isinstance(compile_check, dict) and isinstance(test_check, dict):
            return ""
        return (
            "The latest completed run is missing persisted compile/test validation evidence in report.json. "
            "Re-run or re-validate the task before repo promotion."
        )

    def _repo_promotion_for_objective(self, objective_id: str, linked_tasks: list[Task]) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        project = self.store.get_project(objective.project_id)
        if project is None:
            raise ValueError(f"Unknown project for objective: {objective.project_id}")
        review = self._promotion_review_for_objective(objective.id, linked_tasks)
        override_active = bool(review.get("operator_override"))
        candidate = self._latest_completed_task_for_objective(linked_tasks)
        candidate_payload: dict[str, object] | None = None
        latest_promotion_payload: dict[str, object] | None = self._latest_objective_repo_promotion(objective.id)
        reason = ""
        eligible = False
        source_repo_root = self._objective_source_repo_root(objective.id, linked_tasks)
        unapplied_completed_tasks = self._completed_unapplied_tasks_for_objective(linked_tasks)
        objective_paths = self._objective_repo_file_set(unapplied_completed_tasks)

        if not unapplied_completed_tasks:
            if any(task.status == TaskStatus.COMPLETED for task in linked_tasks):
                reason = "All completed atomic units for this objective have already been promoted."
            else:
                reason = "No completed linked task is available yet."
        else:
            candidate = unapplied_completed_tasks[-1]
            completed_run = self._latest_completed_run(candidate)
            blocker_reason = self._unapplied_repo_promotion_blocker(unapplied_completed_tasks)
            candidate_payload = {
                "task_id": candidate.id,
                "title": candidate.title,
                "status": candidate.status.value,
                "latest_completed_run_id": completed_run.id if completed_run is not None else "",
                "latest_completed_attempt": completed_run.attempt if completed_run is not None else None,
                "unapplied_completed_task_count": len(unapplied_completed_tasks),
                "unapplied_completed_task_ids": [task.id for task in unapplied_completed_tasks],
            }
            if blocker_reason:
                reason = blocker_reason
            elif not objective_paths:
                reason = "Objective promotion could not determine any objective-related file paths to apply."
            elif source_repo_root is None:
                reason = "Objective promotion requires a git-backed source repository root."
            else:
                if not bool(review.get("review_clear")) and not override_active:
                    reason = "Objective review must be clear before repo promotion."
                else:
                    eligible = True
                    reason = (
                        f"Operator override is active. Repo promotion will batch {len(unapplied_completed_tasks)} unapplied completed atomic unit(s) across {len(objective_paths)} tracked file(s) and apply them to the repository."
                        if override_active and not bool(review.get("review_clear"))
                        else f"{len(unapplied_completed_tasks)} unapplied completed atomic unit(s) are ready to promote to the repository across {len(objective_paths)} tracked file(s)."
                    )

        return {
            "eligible": eligible,
            "reason": reason,
            "project_settings": {
                "promotion_mode": project.promotion_mode.value,
                "repo_provider": project.repo_provider.value if project.repo_provider is not None else "",
                "repo_name": project.repo_name,
                "base_branch": project.base_branch,
            },
            "candidate": candidate_payload,
            "latest_promotion": latest_promotion_payload,
        }

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

    def complete_interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        review = self._interrogation_review(objective_id)
        if review.get("completed"):
            return {"interrogation_review": review}
        if review.get("generated_by") == "deterministic":
            review = self._generate_interrogation_review(objective_id)
        self._persist_interrogation_record("interrogation_completed", objective, review)
        self.reconcile_objective_workflow(objective.id)
        return {"interrogation_review": self._interrogation_review(objective.id)}

    def update_mermaid_artifact(
        self,
        objective_id: str,
        *,
        status: str,
        summary: str,
        blocking_reason: str,
        author_type: str = "operator",
        async_generation: bool = True,
    ) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        if getattr(self.ctx, "is_test", False):
            async_generation = False
        normalized = status.strip().lower()
        try:
            next_status = MermaidStatus(normalized)
        except ValueError as exc:
            raise ValueError(f"Unsupported Mermaid status: {status}") from exc

        latest = self.store.latest_mermaid_artifact(objective.id, "workflow_control")
        if latest is None:
            latest = self._create_seed_mermaid(objective)
        content = latest.content if latest is not None else self._default_objective_mermaid(objective)
        artifact = MermaidArtifact(
            id=new_id("diagram"),
            objective_id=objective.id,
            diagram_type="workflow_control",
            version=self.store.next_mermaid_version(objective.id, "workflow_control"),
            status=next_status,
            summary=(summary.strip() or latest.summary or f"{next_status.value} workflow review"),
            content=content,
            required_for_execution=True,
            blocking_reason=blocking_reason.strip(),
            author_type=author_type,
        )
        self.store.create_mermaid_artifact(artifact)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_status_change",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type=author_type,
                content=f"Mermaid workflow_control marked {next_status.value}",
                metadata={
                    "diagram_id": artifact.id,
                    "diagram_type": artifact.diagram_type,
                    "version": artifact.version,
                    "status": artifact.status.value,
                    "required_for_execution": artifact.required_for_execution,
                    "blocking_reason": artifact.blocking_reason,
                },
            )
        )
        if next_status == MermaidStatus.PAUSED:
            self.store.update_objective_status(objective.id, ObjectiveStatus.PAUSED)
        elif next_status == MermaidStatus.FINISHED:
            runner = self.start_objective_lifecycle(objective.id)
            # Ensure phase is at MERMAID_REVIEW before approving.
            # For objectives that haven't been through the runner yet,
            # fast-forward to MERMAID_REVIEW.
            if runner.phase == ObjectivePhase.CREATED:
                runner._advance(ObjectivePhase.INTERROGATING)
                runner._advance(ObjectivePhase.MERMAID_REVIEW)
            runner.approve_mermaid()
            self.store.update_objective_status(objective.id, ObjectiveStatus.PLANNING)
            self.complete_interrogation_review(objective.id)
            self.queue_atomic_generation(objective.id, async_mode=async_generation, runner=runner)
        else:
            self.store.update_objective_status(objective.id, ObjectiveStatus.INVESTIGATING)
        self.reconcile_objective_workflow(objective.id)
        return {"diagram": serialize_dataclass(artifact)}

    def propose_mermaid_update(self, objective_id: str, *, directive: str) -> dict[str, object] | None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        proposal = self._generate_mermaid_update_proposal(objective_id, directive=directive)
        if proposal is None:
            return None
        record = ContextRecord(
            id=new_id("context"),
            record_type="mermaid_update_proposed",
            project_id=objective.project_id,
            objective_id=objective.id,
            visibility="model_visible",
            author_type="system",
            content=proposal["summary"],
            metadata={
                "content": proposal["content"],
                "summary": proposal["summary"],
                "directive": directive,
                "backend": proposal.get("backend", ""),
                "prompt_path": proposal.get("prompt_path", ""),
                "response_path": proposal.get("response_path", ""),
                "red_team_review": proposal.get("red_team_review", ""),
            },
        )
        self.store.create_context_record(record)
        return {
            "id": record.id,
            "summary": record.content,
            "content": str(record.metadata.get("content") or ""),
            "directive": directive,
            "created_at": record.created_at.isoformat(),
        }

    def accept_mermaid_proposal(self, objective_id: str, proposal_id: str, *, async_generation: bool = True) -> dict[str, object]:
        if getattr(self.ctx, "is_test", False):
            async_generation = False
        objective = self.store.get_objective(objective_id)
        proposal = self._proposal_record(objective_id, proposal_id)
        if objective is None or proposal is None:
            raise ValueError("Unknown Mermaid proposal")
        content = str(proposal.metadata.get("content") or "").strip()
        if not content:
            raise ValueError("Mermaid proposal content is empty")
        artifact = MermaidArtifact(
            id=new_id("diagram"),
            objective_id=objective.id,
            diagram_type="workflow_control",
            version=self.store.next_mermaid_version(objective.id, "workflow_control"),
            status=MermaidStatus.FINISHED,
            summary=str(proposal.metadata.get("summary") or proposal.content or "Accepted control flow"),
            content=content,
            required_for_execution=True,
            blocking_reason="",
            author_type="operator",
        )
        self.store.create_mermaid_artifact(artifact)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_status_change",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="operator",
                content="Mermaid workflow_control marked finished",
                metadata={
                    "diagram_id": artifact.id,
                    "diagram_type": artifact.diagram_type,
                    "version": artifact.version,
                    "status": artifact.status.value,
                    "required_for_execution": artifact.required_for_execution,
                    "blocking_reason": artifact.blocking_reason,
                },
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_update_accepted",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="operator",
                content="Accepted proposed Mermaid update.",
                metadata={"proposal_id": proposal.id, "diagram_id": artifact.id, "version": artifact.version},
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
                content=f"Action receipt: Exact proposal on screen promoted unchanged to locked current version {artifact.version}. No regeneration occurred.",
                metadata={
                    "kind": "mermaid_update",
                    "status": "accepted",
                    "proposal_id": proposal.id,
                    "diagram_id": artifact.id,
                    "promotion_mode": "exact_proposal",
                },
            )
        )
        self.store.update_objective_status(objective.id, ObjectiveStatus.PLANNING)
        self.queue_atomic_generation(objective.id, async_mode=async_generation)
        self.reconcile_objective_workflow(objective.id)
        return {"diagram": serialize_dataclass(artifact)}

    def reject_mermaid_proposal(self, objective_id: str, proposal_id: str, *, resolution: str = "refine") -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        proposal = self._proposal_record(objective_id, proposal_id)
        if objective is None or proposal is None:
            raise ValueError("Unknown Mermaid proposal")
        normalized = resolution.strip().lower() or "refine"
        if normalized not in {"refine", "rewind_hard"}:
            raise ValueError(f"Unsupported Mermaid proposal resolution: {resolution}")
        record_type = "mermaid_update_rejected" if normalized == "refine" else "mermaid_update_rewound"
        content = "Keep refining the Mermaid update." if normalized == "refine" else "Rewind the Mermaid update and reconsider from the last approved diagram."
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type=record_type,
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="operator",
                content=content,
                metadata={"proposal_id": proposal.id, "resolution": normalized},
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
                    "Action receipt: Mermaid proposal kept for further refinement."
                    if normalized == "refine"
                    else "Action receipt: Mermaid proposal rewound hard to the last approved diagram."
                ),
                metadata={"kind": "mermaid_update", "status": normalized, "proposal_id": proposal.id},
            )
        )
        self.reconcile_objective_workflow(objective.id)
        return {"rejected": True, "proposal_id": proposal.id, "resolution": normalized}

    def proposed_first_task(self, objective_id: str) -> dict[str, object]:
        linked_objective = self.store.get_objective(objective_id)
        if linked_objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        intent_model = self.store.latest_intent_model(objective_id)
        desired_outcome = (intent_model.intent_summary if intent_model is not None else "").strip()
        success_definition = (intent_model.success_definition if intent_model is not None else "").strip()
        summary = linked_objective.summary.strip()

        if desired_outcome:
            objective_text = desired_outcome
        elif summary:
            objective_text = summary
        else:
            objective_text = linked_objective.title

        if success_definition:
            objective_text = f"{objective_text} Success means: {success_definition}"

        return {
            "title": f"First slice: {linked_objective.title}",
            "objective": f"{objective_text} Keep the slice bounded and operator-visible.",
            "reason": "The harness generated this first slice from the objective, desired outcome, and success definition so you do not need to author the initial task manually.",
        }

    def create_linked_task(self, objective_id: str) -> dict[str, object]:
        linked_objective = self.store.get_objective(objective_id)
        if linked_objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        proposal = self.proposed_first_task(objective_id)
        task = self.task_service.create_task_with_policy(
            project_id=linked_objective.project_id,
            objective_id=linked_objective.id,
            title=str(proposal["title"]),
            objective=str(proposal["objective"]),
            priority=linked_objective.priority,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="generic",
            validation_mode="lightweight_operator",
            scope={},
            strategy="operator_ergonomics",
            max_attempts=3,
            required_artifacts=["plan", "report"],
        )
        self.store.update_objective_phase(linked_objective.id)
        self.reconcile_objective_workflow(linked_objective.id)
        return {"task": serialize_dataclass(task)}

    def queue_atomic_generation(self, objective_id: str, *, async_mode: bool = True, runner: "ObjectiveLifecycleRunner | None" = None) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        if mermaid is None or mermaid.status != MermaidStatus.FINISHED:
            raise ValueError("Atomic generation requires a finished Mermaid.")
        current = self._atomic_generation_state(objective_id)
        if current["status"] == "running" and self._atomic_generation_is_stale(current, objective_id):
            self._mark_atomic_generation_interrupted(objective, current)
            current = self._atomic_generation_state(objective_id)
        if current["status"] == "running" and int(current.get("diagram_version") or 0) == mermaid.version:
            return {"atomic_generation": current}
        if current["status"] == "completed" and int(current.get("diagram_version") or 0) == mermaid.version:
            return {"atomic_generation": current}
        generation_id = new_id("atomic_generation")
        start_record = ContextRecord(
            id=new_id("context"),
            record_type="atomic_generation_started",
            project_id=objective.project_id,
            objective_id=objective.id,
            visibility="operator_visible",
            author_type="system",
            content=f"Started generating atomic units from Mermaid v{mermaid.version}.",
            metadata={"generation_id": generation_id, "diagram_version": mermaid.version},
        )
        self.store.create_context_record(start_record)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Action receipt: Generating atomic units from accepted flowchart v{mermaid.version}.",
                metadata={"kind": "atomic_generation", "status": "started", "generation_id": generation_id, "diagram_version": mermaid.version},
            )
        )
        self._emit_workflow_progress(
            {
                "type": "workflow_stage_changed",
                "stage_kind": "atomic_generation",
                "stage_status": "started",
                "objective_id": objective.id,
                "objective_title": objective.title,
                "generation_id": generation_id,
                "detail": f"Started atomic generation from Mermaid v{mermaid.version}.",
            }
        )

        _runner = runner or self.start_objective_lifecycle(objective.id)

        def worker() -> None:
            self._run_atomic_generation(objective.id, generation_id, mermaid.version, lifecycle_runner=_runner)

        if async_mode:
            _ATOMIC_GENERATION.start(objective.id, worker)
        else:
            worker()
        return {"atomic_generation": self._atomic_generation_state(objective.id)}

    def _atomic_generation_is_stale(self, generation: dict[str, object], objective_id: str = "") -> bool:
        if generation.get("status") != "running":
            return False
        # If the in-memory coordinator thread is still alive, it's not stale
        if objective_id and objective_id in _ATOMIC_GENERATION._running:
            return False
        last_activity_at = str(generation.get("last_activity_at") or "")
        if not last_activity_at:
            return False
        try:
            last_activity = _dt.datetime.fromisoformat(last_activity_at)
        except ValueError:
            return False
        age_seconds = (_dt.datetime.now(_dt.timezone.utc) - last_activity).total_seconds()
        # LLM calls can take several minutes; 5 minutes is a reasonable staleness threshold
        return age_seconds > 300

    def _mark_atomic_generation_interrupted(self, objective: Objective, generation: dict[str, object]) -> None:
        generation_id = str(generation.get("generation_id") or "")
        if not generation_id:
            return
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_failed",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Atomic generation was interrupted before publishing units. The harness can resume from the accepted flowchart.",
                metadata={
                    "generation_id": generation_id,
                    "diagram_version": generation.get("diagram_version"),
                    "interrupted": True,
                },
            )
        )
        self._emit_workflow_progress(
            {
                "type": "workflow_stage_changed",
                "stage_kind": "atomic_generation",
                "stage_status": "interrupted",
                "objective_id": objective.id,
                "objective_title": objective.title,
                "generation_id": generation_id,
                "detail": "Atomic generation was interrupted and is eligible for restart.",
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
                content="Action receipt: Atomic generation was interrupted. Resuming from the accepted flowchart.",
                metadata={
                    "kind": "atomic_generation",
                    "status": "interrupted",
                    "generation_id": generation_id,
                    "diagram_version": generation.get("diagram_version"),
                },
            )
        )

    def _maybe_resume_atomic_generation(self, objective_id: str) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        if mermaid is None or mermaid.status != MermaidStatus.FINISHED:
            return
        generation = self._atomic_generation_state(objective_id)
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective_id]
        if generation.get("status") == "running" and self._atomic_generation_is_stale(generation, objective_id):
            self._mark_atomic_generation_interrupted(objective, generation)
            generation = self._atomic_generation_state(objective_id)
        if generation.get("status") == "completed":
            return
        if generation.get("status") == "running" and not self._atomic_generation_is_stale(generation, objective_id):
            return
        has_runnable_linked_work = any(task.status in {TaskStatus.PENDING, TaskStatus.ACTIVE} for task in linked_tasks)
        if linked_tasks and has_runnable_linked_work:
            return
        self.queue_atomic_generation(objective_id, async_mode=self._workflow_async_mode())

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
            from .services.objective_review_orchestrator import ObjectiveReviewOrchestrator

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
                    packet.setdefault("llm_usage", llm_usage)
                    packet.setdefault("llm_usage_reported", usage_reported)
                    packet.setdefault("llm_usage_source", usage_source)
                    packet.setdefault("review_task_id", "")
                    packet.setdefault("review_run_id", "")
                return packets
        return self._deterministic_objective_review_packets(objective_payload)

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

    def _run_atomic_generation(self, objective_id: str, generation_id: str, diagram_version: int, *, lifecycle_runner=None) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        _lr = lifecycle_runner
        try:
            self._record_atomic_generation_progress(
                objective,
                generation_id,
                diagram_version,
                phase="reading accepted flowchart",
                content=f"Reading accepted Mermaid v{diagram_version} before decomposition.",
            )
            if _lr is not None and _lr.phase == ObjectivePhase.MERMAID_REVIEW:
                _lr._advance(ObjectivePhase.TRIO_PLANNING)
            self._record_atomic_generation_progress(
                objective,
                generation_id,
                diagram_version,
                phase="running TRIO planning",
                content="Running TRIO plan decomposition with red-team review.",
            )
            trio_result = self._generate_trio_plans_for_objective(objective)
            if not trio_result.success or not trio_result.plans:
                raise RuntimeError(
                    f"TRIO planning failed after {trio_result.rounds_completed} round(s): "
                    f"{trio_result.stop_reason}"
                )
            plans_data = trio_result.plans
            from .skills.plan_draft import materialize_plans_from_skill_output
            materialized = materialize_plans_from_skill_output(
                self.store, objective.id, plans_data, author_tag="plan_draft_trio",
            )
            self._record_atomic_generation_progress(
                objective,
                generation_id,
                diagram_version,
                phase="publishing units",
                content=f"Publishing {len(materialized)} TRIO plans as tasks.",
            )
            for index, plan in enumerate(materialized, start=1):
                sl = plan.slice or {}
                target_impl = str(sl.get("target_impl") or "").split("::", 1)[0].strip()
                target_test = str(sl.get("target_test") or "").split("::", 1)[0].strip()
                files_to_touch = [p for p in (target_impl, target_test) if p]
                scope = {
                    "files_to_touch": files_to_touch,
                    "files_not_to_touch": [],
                    "approach": str(sl.get("transformation") or sl.get("label") or ""),
                    "risks": list(sl.get("risks") or []),
                    "estimated_complexity": str(sl.get("estimated_complexity") or "medium"),
                }
                task = self.task_service.create_task_with_policy(
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    title=str(sl.get("label") or f"Plan {plan.id}"),
                    objective=str(sl.get("transformation") or sl.get("label") or ""),
                    priority=objective.priority,
                    parent_task_id=None,
                    source_run_id=None,
                    external_ref_type=None,
                    external_ref_id=None,
                    validation_profile="generic",
                    validation_mode="lightweight_operator",
                    scope=scope,
                    strategy="trio_plan",
                    max_attempts=3,
                    required_artifacts=["plan", "report"],
                    mermaid_node_id=plan.mermaid_node_id,
                    plan_id=plan.id,
                )
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="atomic_unit_generated",
                        project_id=objective.project_id,
                        objective_id=objective.id,
                        task_id=task.id,
                        visibility="operator_visible",
                        author_type="system",
                        content=f"Generated TRIO plan {index}: {task.title}",
                        metadata={
                            "generation_id": generation_id,
                            "diagram_version": diagram_version,
                            "order": index,
                            "task_id": task.id,
                            "plan_id": plan.id,
                            "title": task.title,
                            "objective": task.objective,
                            "target_impl": sl.get("target_impl") or "",
                            "target_test": sl.get("target_test") or "",
                            "strategy": task.strategy,
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
                        content=f"Action receipt: Published TRIO plan {index}: {task.title}",
                        metadata={
                            "kind": "atomic_generation",
                            "status": "publishing",
                            "generation_id": generation_id,
                            "diagram_version": diagram_version,
                            "order": index,
                            "task_id": task.id,
                            "plan_id": plan.id,
                        },
                    )
                )
                time.sleep(0.12)
            if _lr is not None and _lr.phase == ObjectivePhase.TRIO_PLANNING:
                _lr._advance(ObjectivePhase.EXECUTING)
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="atomic_generation_completed",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Generated {len(materialized)} TRIO plans from Mermaid v{diagram_version}.",
                    metadata={"generation_id": generation_id, "diagram_version": diagram_version, "unit_count": len(materialized)},
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
                    content=f"Action receipt: TRIO generation complete. {len(materialized)} plans are ready for review.",
                    metadata={"kind": "atomic_generation", "status": "completed", "generation_id": generation_id, "unit_count": len(materialized)},
                )
            )
            self._emit_workflow_progress(
                {
                    "type": "workflow_stage_changed",
                    "stage_kind": "atomic_generation",
                    "stage_status": "completed",
                    "objective_id": objective.id,
                    "objective_title": objective.title,
                    "generation_id": generation_id,
                    "detail": f"Generated {len(materialized)} TRIO plan(s) from Mermaid v{diagram_version}.",
                }
            )
            self.store.update_objective_phase(objective.id)
            if self.auto_resume_atomic_generation:
                _BACKGROUND_SUPERVISOR.start(objective.project_id, self.ctx.engine, watch=True)
        except Exception as exc:
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="atomic_generation_failed",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Atomic generation failed: {exc}",
                    metadata={"generation_id": generation_id, "diagram_version": diagram_version},
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
                    content="Action receipt: Atomic generation failed. Ask the harness to retry or revise the flowchart decomposition.",
                    metadata={"kind": "atomic_generation", "status": "failed", "generation_id": generation_id},
                )
            )
            self._emit_workflow_progress(
                {
                    "type": "workflow_stage_changed",
                    "stage_kind": "atomic_generation",
                    "stage_status": "failed",
                    "objective_id": objective.id,
                    "objective_title": objective.title,
                    "generation_id": generation_id,
                    "detail": f"Atomic generation failed: {exc}",
                }
            )
            self.store.update_objective_status(objective.id, ObjectiveStatus.PAUSED)

    def _record_atomic_generation_progress(
        self,
        objective: Objective,
        generation_id: str,
        diagram_version: int,
        *,
        phase: str,
        content: str,
    ) -> None:
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_progress",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=content,
                metadata={"generation_id": generation_id, "diagram_version": diagram_version, "phase": phase},
            )
        )
        self._emit_workflow_progress(
            {
                "type": "workflow_stage_changed",
                "stage_kind": "atomic_generation",
                "stage_status": "progress",
                "objective_id": objective.id,
                "objective_title": objective.title,
                "generation_id": generation_id,
                "detail": f"Atomic generation phase: {phase}.",
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
                content=f"Action receipt: Atomic generation phase changed to {phase}.",
                metadata={
                    "kind": "atomic_generation",
                    "status": "progress",
                    "generation_id": generation_id,
                    "diagram_version": diagram_version,
                    "phase": phase,
                },
            )
        )

    def _generate_trio_plans_for_objective(self, objective):
        """Run trio_plan_orchestrator.generate_trio_plans for an objective.

        Gathers intent model, interrogation context, and builds the
        SkillContext from the project's source root so TRIO plans are
        grounded against the real repo inventory.
        """
        from .services.trio_plan_orchestrator import generate_trio_plans
        from .skills.context import build_default_skill_context

        intent_model = self.store.latest_intent_model(objective.id)
        source_root = self._resolve_source_root(objective.project_id)
        skill_context = build_default_skill_context(source_root)

        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is None or not getattr(llm_router, "executors", {}):
            raise RuntimeError("No LLM router available for TRIO planning")

        intent_inputs = {
            "objective_title": objective.title,
            "objective_summary": objective.summary,
            "intent_summary": intent_model.intent_summary if intent_model else "",
            "success_definition": intent_model.success_definition if intent_model else "",
            "non_negotiables": list(intent_model.non_negotiables) if intent_model else [],
            "frustration_signals": list(getattr(intent_model, "frustration_signals", []) or []),
        }
        return generate_trio_plans(
            intent_inputs=intent_inputs,
            project_id=objective.project_id,
            objective_id=objective.id,
            skill_context=skill_context,
            llm_router=llm_router,
            store=self.store,
            workspace_root=self.workspace_root,
            telemetry=getattr(self.ctx, "telemetry", None),
        )

    def _resolve_source_root(self, project_id: str) -> Path:
        """Resolve the source repo root for a project."""
        project = self.store.get_project(project_id)
        if project and project.adapter_name == "current_repo_git_worktree":
            configured = os.environ.get("ACCRUVIA_SOURCE_REPO_ROOT")
            if configured:
                return Path(configured).resolve()
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    check=True, capture_output=True, text=True,
                )
                return Path(result.stdout.strip())
            except Exception:
                pass
        return Path(__file__).resolve().parents[2]

    def run_task(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        run = self.ctx.engine.run_once(task.id)
        return {"run": serialize_dataclass(run)}

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

    def retry_task(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task.status.value != "failed":
            raise ValueError(f"Task is {task.status.value}, not failed")
        self.store.update_task_status(task_id, TaskStatus.PENDING)
        engine = getattr(self.ctx, "engine", None)
        if engine is not None:
            _BACKGROUND_SUPERVISOR.start(task.project_id, engine, watch=True)
        return {"task_id": task_id, "status": "pending"}

    def apply_failed_task_disposition(
        self,
        task_id: str,
        *,
        disposition: str,
        rationale: str,
    ) -> dict[str, object]:
        result = self.task_service.apply_failed_task_disposition(
            task_id=task_id,
            disposition=disposition,
            rationale=rationale,
        )
        task = self.store.get_task(task_id)
        engine = getattr(self.ctx, "engine", None)
        if task is not None and engine is not None and disposition.strip().lower() in {"retry_as_is", "allow_manual_operator_implementation"}:
            _BACKGROUND_SUPERVISOR.start(task.project_id, engine, watch=True)
        return result

    def _auto_retry_restart_safe_failed_task(self, task: Task) -> bool:
        if task.status != TaskStatus.FAILED:
            return False
        runs = self.store.list_runs(task.id)
        if not runs:
            return False
        latest_run = runs[-1]
        metadata = dict(task.external_ref_metadata) if isinstance(task.external_ref_metadata, dict) else {}
        triage = metadata.get("auto_restart_triage") if isinstance(metadata.get("auto_restart_triage"), dict) else {}
        if str(triage.get("source_run_id") or "") == latest_run.id and task.status in {TaskStatus.PENDING, TaskStatus.ACTIVE}:
            return False

        reason = ""
        if latest_run.summary == "Recovered: process crash detected" and latest_run.attempt < task.max_attempts:
            reason = "recovered_process_crash"
        else:
            evaluations = self.store.list_evaluations(latest_run.id)
            latest_evaluation = evaluations[-1] if evaluations else None
            details = latest_evaluation.details if latest_evaluation is not None and isinstance(latest_evaluation.details, dict) else {}
            diagnostics = details.get("diagnostics") if isinstance(details.get("diagnostics"), dict) else {}
            failure_category = str(diagnostics.get("failure_category") or "").strip()
            infrastructure_failure = bool(diagnostics.get("infrastructure_failure"))
            restart_safe_categories = {"executor_process_failure", "executor_timeout", "llm_executor_failure", "workspace_contract_failure"}
            if infrastructure_failure and failure_category in restart_safe_categories and latest_run.attempt < task.max_attempts:
                reason = failure_category

        if not reason:
            return False

        metadata["auto_restart_triage"] = {
            "disposition": "retry_as_is",
            "reason": reason,
            "source_run_id": latest_run.id,
            "source_attempt": latest_run.attempt,
            "requeued_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        self.store.update_task_external_metadata(task.id, metadata)
        self.store.update_task_status(task.id, TaskStatus.PENDING)
        if task.objective_id:
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="action_receipt",
                    project_id=task.project_id,
                    objective_id=task.objective_id,
                    task_id=task.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Action receipt: Automatically requeued restart-safe failed task {task.title}.",
                    metadata={"kind": "failed_task_auto_requeued", "task_id": task.id, "source_run_id": latest_run.id, "reason": reason},
                )
            )
        engine = getattr(self.ctx, "engine", None)
        if engine is not None:
            _BACKGROUND_SUPERVISOR.start(task.project_id, engine, watch=True)
        return True

    def retry_all_failed(self, project_id: str) -> dict[str, object]:
        # Check LLM availability via the central gate before requeuing.
        gate = self.ctx.engine.llm_gate
        gate.reset()  # Force a fresh probe.
        if not gate.is_available():
            raise ValueError(f"No LLM backends available. Probes: {gate.last_probe_results}")

        tasks = self.store.list_tasks(project_id=project_id)
        reset_count = 0
        for task in tasks:
            if task.status == TaskStatus.FAILED:
                self.store.update_task_status(task.id, TaskStatus.PENDING)
                reset_count += 1
        engine = getattr(self.ctx, "engine", None)
        if reset_count > 0 and engine is not None:
            _BACKGROUND_SUPERVISOR.start(project_id, engine, watch=True)
        return {"reset_count": reset_count, "probe_results": gate.last_probe_results}

    def start_supervisor(self, project_id: str) -> dict[str, object]:
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        started = _BACKGROUND_SUPERVISOR.start(project_id, self.ctx.engine, watch=True)
        return {
            "started": started,
            "supervisor": _BACKGROUND_SUPERVISOR.status(project_id),
        }

    def stop_supervisor(self, project_id: str) -> dict[str, object]:
        stopped = _BACKGROUND_SUPERVISOR.stop(project_id)
        return {
            "stopped": stopped,
            "supervisor": _BACKGROUND_SUPERVISOR.status(project_id),
        }

    def supervisor_status(self, project_id: str) -> dict[str, object]:
        return {
            "running": _BACKGROUND_SUPERVISOR.is_running(project_id),
            "supervisor": _BACKGROUND_SUPERVISOR.status(project_id),
        }

    def harness_overview(self) -> dict[str, object]:
        with self._harness_overview_cache_lock:
            cached = self._harness_overview_cache
            if cached is not None and (time.monotonic() - cached[0]) < 5.0:
                return cached[1]
        payload = self._build_harness_overview()
        with self._harness_overview_cache_lock:
            self._harness_overview_cache = (time.monotonic(), payload)
        return payload

    def _harness_workflow_status_for_objective(
        self,
        objective: Objective,
        linked_tasks: list[Task],
    ) -> dict[str, object]:
        planning = self.workflow_service.planning_readiness(objective.id)
        execution = self.workflow_service.execution_readiness(objective.id, linked_tasks)
        review = self.workflow_service.review_readiness(objective.id, linked_tasks)
        current_stage = (
            "review"
            if objective.status == ObjectiveStatus.RESOLVED
            else "execution"
            if objective.status == ObjectiveStatus.EXECUTING
            else "planning"
        )
        return {
            "planning": {
                "stage": planning.stage,
                "ready": planning.ready,
                "checks": _to_jsonable(planning.checks),
            },
            "execution": {
                "stage": execution.stage,
                "ready": execution.ready,
                "checks": _to_jsonable(execution.checks),
            },
            "review": {
                "stage": review.stage,
                "ready": review.ready,
                "checks": _to_jsonable(review.checks),
            },
            "current_stage": current_stage,
        }

    def _build_harness_overview(self) -> dict[str, object]:
        """System-wide harness dashboard data."""
        projects = []
        global_counts = {"completed": 0, "active": 0, "failed": 0, "pending": 0}
        active_objectives: list[dict[str, object]] = []
        projects_list = self.store.list_projects()
        tasks_by_project: dict[str, list[Task]] = {}
        for project in projects_list:
            tasks_by_project[project.id] = self.store.list_tasks(project.id)
        for project in projects_list:
            metrics = self.store.metrics_snapshot(project.id)
            tasks_by_status = metrics.get("tasks_by_status", {})
            for status_key in global_counts:
                global_counts[status_key] += int(tasks_by_status.get(status_key, 0))
            objectives = self.store.list_objectives(project.id)
            all_project_tasks = tasks_by_project[project.id]
            active_objective = None
            all_objectives = []
            blocked_pending = 0
            waiting_on_review = 0
            runnable_pending = 0
            for obj in objectives:
                linked_tasks = [t for t in all_project_tasks if t.objective_id == obj.id]
                task_counts = {"completed": 0, "active": 0, "failed": 0, "pending": 0}
                for t in linked_tasks:
                    s = t.status.value if hasattr(t.status, "value") else str(t.status)
                    if s in task_counts:
                        task_counts[s] += 1
                active_task_titles = [t.title for t in linked_tasks if t.status == TaskStatus.ACTIVE]
                needs_workflow = bool(active_task_titles) or task_counts["pending"] > 0 or obj.status in {
                    ObjectiveStatus.EXECUTING,
                    ObjectiveStatus.PLANNING,
                }
                workflow = (
                    self._harness_workflow_status_for_objective(obj, linked_tasks)
                    if needs_workflow
                    else None
                )
                review_ready = bool((workflow or {}).get("review", {}).get("ready"))
                for t in linked_tasks:
                    s = t.status.value if hasattr(t.status, "value") else str(t.status)
                    if s == TaskStatus.PENDING.value:
                        queue_state = self.workflow_service.queue_state_for_task(t, review_ready=review_ready)
                        state = str(queue_state.get("state") or "")
                        if state == "blocked_by_gate":
                            blocked_pending += 1
                        elif state == "waiting_on_review":
                            waiting_on_review += 1
                        elif state == "runnable":
                            runnable_pending += 1
                obj_data = {
                    "id": obj.id,
                    "project_id": project.id,
                    "project_name": project.name,
                    "title": obj.title,
                    "status": obj.status.value,
                    "task_counts": task_counts,
                    "task_total": len(linked_tasks),
                }
                all_objectives.append(obj_data)
                if active_task_titles or task_counts["pending"] > 0 or obj.status in {ObjectiveStatus.EXECUTING, ObjectiveStatus.PLANNING}:
                    active_objectives.append(
                        {
                            **obj_data,
                            "workflow": workflow
                            or {"planning": {"checks": []}, "review": {"checks": []}},
                            "active_task_titles": active_task_titles,
                        }
                    )
                if active_objective is None and obj.status.value in ("executing", "planning"):
                    gen = self._atomic_generation_state(obj.id)
                    active_objective = {**obj_data, "atomic_generation": gen}
            supervisor = _BACKGROUND_SUPERVISOR.status(project.id)
            external_supervisors = self._live_supervisor_records(project.id)
            in_process_running = _BACKGROUND_SUPERVISOR.is_running(project.id)
            running = in_process_running or bool(external_supervisors)
            supervisor_state = supervisor.get("state", "idle")
            if not in_process_running and external_supervisors:
                supervisor_state = "running"
            projects.append({
                "id": project.id,
                "name": project.name,
                "supervisor": {
                    **supervisor,
                    "running": running,
                    "state": supervisor_state,
                    "external_supervisor_count": len(external_supervisors),
                    "external_supervisors": external_supervisors,
                },
                "tasks_by_status": dict(tasks_by_status),
                "pending_queue_states": {
                    "runnable": runnable_pending,
                    "blocked_by_gate": blocked_pending,
                    "waiting_on_review": waiting_on_review,
                },
                "task_total": sum(int(v) for v in tasks_by_status.values()),
                "active_objective": active_objective,
                "objectives": all_objectives,
            })
        # LLM health from router
        llm_health = []
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is not None:
            for name in sorted(llm_router.executors.keys()):
                llm_health.append({
                    "name": name,
                    "demoted": name in llm_router._demoted,
                })
        # Recent events for the feed
        recent_events = []
        for project in projects_list:
            records = self.store.list_context_records(
                project_id=project.id, record_type="action_receipt",
            )
            for record in records[-20:]:
                text = record.content
                if text.startswith("Action receipt: "):
                    text = text[len("Action receipt: "):]
                recent_events.append({
                    "project_id": project.id,
                    "project_name": project.name,
                    "text": text,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id or "",
                    "task_id": record.task_id or "",
                })
            # Also include decomposition telemetry
            telemetry = self.store.list_context_records(
                project_id=project.id, record_type="atomic_decomposition_telemetry",
            )
            for record in telemetry[-10:]:
                recent_events.append({
                    "project_id": project.id,
                    "project_name": project.name,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id or "",
                    "task_id": record.task_id or "",
                })
            # Include completed and failed task events
            all_tasks = tasks_by_project[project.id]
            for t in all_tasks:
                status_val = t.status.value if hasattr(t.status, "value") else str(t.status)
                if status_val == "completed":
                    recent_events.append({
                        "project_id": project.id,
                        "project_name": project.name,
                        "text": f"Task completed: {t.title}",
                        "created_at": t.updated_at.isoformat(),
                        "objective_id": t.objective_id or "",
                        "task_id": t.id,
                        "event_type": "task_completed",
                    })
                elif status_val == "failed":
                    recent_events.append({
                        "project_id": project.id,
                        "project_name": project.name,
                        "text": f"Task failed: {t.title}",
                        "created_at": t.updated_at.isoformat(),
                        "objective_id": t.objective_id or "",
                        "task_id": t.id,
                        "event_type": "task_failed",
                    })
                elif status_val == "active":
                    recent_events.append({
                        "project_id": project.id,
                        "project_name": project.name,
                        "text": f"Task started: {t.title}",
                        "created_at": t.updated_at.isoformat(),
                        "objective_id": t.objective_id or "",
                        "task_id": t.id,
                        "event_type": "task_active",
                    })
        recent_events.sort(key=lambda e: e["created_at"], reverse=True)
        active_objectives.sort(
            key=lambda item: (
                -(int((item.get("task_counts") or {}).get("active", 0))),
                -(int((item.get("task_counts") or {}).get("pending", 0))),
                0 if item.get("status") == ObjectiveStatus.EXECUTING.value else 1,
                str(item.get("project_name") or ""),
                str(item.get("title") or ""),
            )
        )
        return {
            "global_counts": global_counts,
            "global_total": sum(global_counts.values()),
            "active_objectives": active_objectives,
            "projects": projects,
            "llm_health": llm_health,
            "recent_events": recent_events[:50],
        }

    def run_cli_command(self, command: str) -> dict[str, object]:
        cleaned = command.strip()
        if not cleaned:
            raise ValueError("CLI command must not be empty")
        command_parts = shlex.split(cleaned)
        repo_root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        src_path = str(repo_root / "src")
        existing_pythonpath = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else src_path
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "accruvia_harness",
                "--db",
                str(self.ctx.config.db_path),
                "--workspace",
                str(self.ctx.config.workspace_root),
                *command_parts,
            ],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if stdout and stderr:
            output = f"{stdout}\n\n[stderr]\n{stderr}"
        else:
            output = stdout or stderr or "(no output)"
        return {
            "command": cleaned,
            "exit_code": completed.returncode,
            "output": output,
        }

    def _ensure_first_linked_task(self, objective_id: str) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        if any(task.objective_id == objective.id for task in self.store.list_tasks(objective.project_id)):
            return
        task_payload = self.create_linked_task(objective.id)
        task = task_payload["task"]
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="task_created",
                project_id=objective.project_id,
                objective_id=objective.id,
                task_id=str(task.get("id") or ""),
                visibility="model_visible",
                author_type="system",
                content=f"Created first bounded slice for objective {objective.title}",
                metadata={
                    "task_title": str(task.get("title") or ""),
                    "strategy": str(task.get("strategy") or ""),
                    "generated_from": "intent_and_mermaid",
                },
            )
        )

    def run_cli_output(self, run_id: str) -> dict[str, object]:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"Unknown run: {run_id}")
        sections_raw = self._run_output_sections(run_id)
        sections = [
            {
                "label": section.label,
                "path": section.path,
                "content": section.content,
            }
            for section in sections_raw
        ]
        return {
            "run": serialize_dataclass(run),
            "summary": self._summarize_run_output(run, sections_raw),
            "sections": sections,
        }

    def add_operator_comment(
        self,
        project_ref: str,
        text: str,
        author: str | None,
        objective_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        body = text.strip()
        if not body:
            raise ValueError("Comment text must not be empty")
        if objective_id:
            objective = self.store.get_objective(objective_id)
            if objective is None or objective.project_id != project.id:
                raise ValueError(f"Unknown objective: {objective_id}")
        selected_task = None
        if task_id:
            selected_task = self.store.get_task(task_id)
            if selected_task is None or selected_task.project_id != project.id:
                raise ValueError(f"Unknown task: {task_id}")
            if objective_id and selected_task.objective_id != objective_id:
                raise ValueError(f"Task {task_id} does not belong to objective {objective_id}")
            if objective_id is None:
                objective_id = selected_task.objective_id
        record = self.context_recorder.record_operator_comment(
            project_id=project.id,
            objective_id=objective_id,
            task_id=task_id,
            author=author,
            content=body,
        )
        if task_id:
            return self._enqueue_task_question(
                project_id=project.id,
                objective_id=objective_id,
                task_id=task_id,
                comment_record=record,
                frustration_detected=self._comment_looks_like_frustration(body),
            )
        if objective_id and self._should_auto_complete_interrogation(objective_id):
            self.complete_interrogation_review(objective_id)
        frustration_detected = self._comment_looks_like_frustration(body)
        mermaid_update_requested = False
        if objective_id:
            mermaid_update_requested = self._comment_requests_mermaid_update(
                body,
                project_id=project.id,
                objective_id=objective_id,
            )
        responder_result = self._answer_operator_comment(
            project_id=project.id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=body,
            frustration_detected=frustration_detected,
        )
        proposal = None
        if objective_id and mermaid_update_requested:
            responder_result.reply = (
                responder_result.reply.rstrip()
                + "\n\nGenerating a proposed Mermaid update from your instruction — this will appear shortly."
            )
            responder_result.recommended_action = "review_mermaid"
            _proposal_objective_id = objective_id
            _proposal_project_id = project.id
            _proposal_directive = body

            def _generate_proposal() -> dict[str, object] | None:
                result = self.propose_mermaid_update(_proposal_objective_id, directive=_proposal_directive)
                receipt_content = (
                    "Action receipt: Mermaid proposal generated."
                    if result is not None
                    else "Action receipt: Mermaid update was requested but no proposal was generated."
                )
                receipt_status = "proposal_generated" if result is not None else "not_applied"
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="action_receipt",
                        project_id=_proposal_project_id,
                        objective_id=_proposal_objective_id,
                        visibility="operator_visible",
                        author_type="system",
                        content=receipt_content,
                        metadata={"kind": "mermaid_update", "status": receipt_status},
                    )
                )
                return result

            if getattr(self.ctx, "is_test", False):
                try:
                    proposal = _generate_proposal()
                except Exception:
                    proposal = None
            else:
                # Run Mermaid proposal in background so the text reply returns immediately.
                def _generate_proposal_background() -> None:
                    try:
                        _generate_proposal()
                    except Exception:
                        pass

                threading.Thread(target=_generate_proposal_background, daemon=True).start()
        self._log_ui_memory_retrieval(
            project_id=project.id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=body,
            responder_result=responder_result,
        )
        if frustration_detected:
            triage = triage_frustration(self.store, project_id=project.id, objective_id=objective_id)
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="operator_frustration",
                    project_id=project.id,
                    objective_id=objective_id,
                    visibility="model_visible",
                    author_type="operator",
                    author_id=(author or "").strip(),
                    content=body,
                    metadata={
                        "triage": {
                            "objective_id": triage.objective_id,
                            "likely_causes": triage.likely_causes,
                            "recommendation": triage.recommendation,
                            "confidence": triage.confidence,
                        },
                        "derived_from": "operator_comment",
                    },
                )
            )
            if objective_id:
                self.store.update_objective_status(objective_id, ObjectiveStatus.INVESTIGATING)
        reply_record = ContextRecord(
            id=new_id("context"),
            record_type="harness_reply",
            project_id=project.id,
            objective_id=objective_id,
            task_id=task_id,
            visibility="operator_visible",
            author_type="system",
            content=responder_result.reply,
            metadata={
                "reply_to": record.id,
                "recommended_action": responder_result.recommended_action,
                "evidence_refs": responder_result.evidence_refs,
                "mode_shift": responder_result.mode_shift,
                "retrieved_memories": [serialize_dataclass(memory) for memory in responder_result.retrieved_memories],
                "llm_backend": responder_result.llm_backend,
                "prompt_path": responder_result.prompt_path,
                "response_path": responder_result.response_path,
            },
        )
        self.store.create_context_record(reply_record)
        return {
            "comment": {
                "id": record.id,
                "author": record.author_id,
                "text": record.content,
                "objective_id": record.objective_id,
                "task_id": record.task_id,
                "created_at": record.created_at.isoformat(),
            },
            "reply": {
                "id": reply_record.id,
                "text": reply_record.content,
                "objective_id": reply_record.objective_id,
                "task_id": reply_record.task_id,
                "created_at": reply_record.created_at.isoformat(),
                "recommended_action": responder_result.recommended_action,
                "evidence_refs": responder_result.evidence_refs,
                "mode_shift": responder_result.mode_shift,
                "retrieved_memories": [serialize_dataclass(memory) for memory in responder_result.retrieved_memories],
                "llm_backend": responder_result.llm_backend,
                "prompt_path": responder_result.prompt_path,
                "response_path": responder_result.response_path,
            },
            "frustration_detected": frustration_detected,
            "mermaid_proposal": proposal,
        }

    def _enqueue_task_question(
        self,
        *,
        project_id: str,
        objective_id: str | None,
        task_id: str,
        comment_record: ContextRecord,
        frustration_detected: bool,
    ) -> dict[str, object]:
        queued_at = _dt.datetime.now(_dt.timezone.utc)
        job_id = new_id("replyjob")
        pending_record = ContextRecord(
            id=new_id("context"),
            record_type="harness_reply_pending",
            project_id=project_id,
            objective_id=objective_id,
            task_id=task_id,
            visibility="operator_visible",
            author_type="system",
            content="Waiting on harness response…",
            metadata={
                "reply_to": comment_record.id,
                "status": "pending",
                "job_id": job_id,
                "queued_at": queued_at.isoformat(),
            },
        )
        self.store.create_context_record(pending_record)

        mp.Process(
            target=_run_task_question_job,
            kwargs={
                "db_path": str(self.ctx.config.db_path),
                "workspace_root": str(self.ctx.config.workspace_root),
                "log_path": (str(self.ctx.config.log_path) if self.ctx.config.log_path is not None else None),
                "config_file": None,
                "project_id": project_id,
                "objective_id": objective_id,
                "task_id": task_id,
                "comment_record_id": comment_record.id,
                "comment_text": comment_record.content,
                "frustration_detected": frustration_detected,
                "job_id": job_id,
                "queued_at_iso": queued_at.isoformat(),
            },
            daemon=True,
        ).start()
        return {
            "comment": {
                "id": comment_record.id,
                "author": comment_record.author_id,
                "text": comment_record.content,
                "objective_id": comment_record.objective_id,
                "task_id": comment_record.task_id,
                "created_at": comment_record.created_at.isoformat(),
            },
            "reply": {
                "id": pending_record.id,
                "text": pending_record.content,
                "objective_id": pending_record.objective_id,
                "task_id": pending_record.task_id,
                "created_at": pending_record.created_at.isoformat(),
                "status": "pending",
                "job_id": job_id,
                "queued_at": queued_at.isoformat(),
            },
            "frustration_detected": frustration_detected,
        }

    def add_operator_frustration(
        self,
        project_ref: str,
        text: str,
        author: str | None,
        objective_id: str | None = None,
    ) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        body = text.strip()
        if not body:
            raise ValueError("Frustration text must not be empty")
        if objective_id:
            objective = self.store.get_objective(objective_id)
            if objective is None or objective.project_id != project.id:
                raise ValueError(f"Unknown objective: {objective_id}")
        triage = triage_frustration(self.store, project_id=project.id, objective_id=objective_id)
        record = ContextRecord(
            id=new_id("context"),
            record_type="operator_frustration",
            project_id=project.id,
            objective_id=objective_id,
            visibility="model_visible",
            author_type="operator",
            author_id=(author or "").strip(),
            content=body,
            metadata={
                "triage": {
                    "objective_id": triage.objective_id,
                    "likely_causes": triage.likely_causes,
                    "recommendation": triage.recommendation,
                    "confidence": triage.confidence,
                }
            },
        )
        self.store.create_context_record(record)
        if objective_id:
            self.store.update_objective_status(objective_id, ObjectiveStatus.INVESTIGATING)
        return {
            "frustration": {
                "id": record.id,
                "author": record.author_id,
                "text": record.content,
                "objective_id": record.objective_id,
                "created_at": record.created_at.isoformat(),
                "triage": record.metadata["triage"],
            }
        }

    def _operator_comments(self, project_id: str) -> list[dict[str, object]]:
        comments = []
        for record in self.store.list_context_records(project_id=project_id, record_type="operator_comment"):
            comments.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "author": record.author_id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                }
            )
        return comments

    def _operator_frustrations(self, project_id: str) -> list[dict[str, object]]:
        frustrations = []
        for record in self.store.list_context_records(project_id=project_id, record_type="operator_frustration"):
            triage = record.metadata.get("triage", {})
            frustrations.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "author": record.author_id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "triage": triage,
                }
            )
        return frustrations

    def _action_receipts(self, project_id: str) -> list[dict[str, object]]:
        receipts = []
        for record in self.store.list_context_records(project_id=project_id, record_type="action_receipt"):
            text = record.content
            if text.startswith("Action receipt: "):
                text = text[len("Action receipt: "):]
            receipts.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "text": text,
                    "created_at": record.created_at.isoformat(),
                    "metadata": record.metadata,
                }
            )
        return receipts

    def _harness_replies(self, project_id: str) -> list[dict[str, object]]:
        replies = []
        for record in self.store.list_context_records(project_id=project_id, record_type="harness_reply"):
            replies.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "recommended_action": record.metadata.get("recommended_action", "none"),
                    "evidence_refs": record.metadata.get("evidence_refs", []),
                    "mode_shift": record.metadata.get("mode_shift", "none"),
                    "retrieved_memories": record.metadata.get("retrieved_memories", []),
                    "llm_backend": record.metadata.get("llm_backend", ""),
                    "prompt_path": record.metadata.get("prompt_path", ""),
                    "response_path": record.metadata.get("response_path", ""),
                }
            )
        return replies

    def _answer_operator_comment(
        self,
        *,
        project_id: str,
        objective_id: str | None,
        task_id: str | None,
        comment_text: str,
        frustration_detected: bool,
    ) -> ResponderResult:
        packet = self._build_responder_context_packet(
            project_id=project_id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=comment_text,
            frustration_detected=frustration_detected,
        )
        llm_result = self._answer_operator_comment_with_llm(
            packet=packet,
            project_id=project_id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=comment_text,
        )
        if llm_result is not None:
            return llm_result
        return ResponderResult(
            reply="Acknowledged. No LLM backend is available for a detailed response.",
            recommended_action="",
            evidence_refs=[],
            mode_shift="",
            retrieved_memories=[],
            llm_backend="",
            prompt_path="",
            response_path="",
        )

    def _answer_operator_comment_with_llm(
        self,
        *,
        packet: ResponderContextPacket,
        project_id: str,
        objective_id: str | None,
        task_id: str | None,
        comment_text: str,
    ) -> ResponderResult | None:
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is None or not getattr(llm_router, "executors", {}):
            return None
        prompt = self._build_ui_responder_prompt(
            packet=packet,
            project_id=project_id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=comment_text,
        )
        run_dir = self.workspace_root / "ui_responder" / (objective_id or project_id) / new_id("reply")
        run_dir.mkdir(parents=True, exist_ok=True)
        task = Task(
            id=new_id("ui_reply_task"),
            project_id=project_id,
            title=f"UI response for {packet.objective.title if packet.objective else packet.project_name}",
            objective="Answer the operator directly from current harness state and full available context.",
            strategy="ui_responder",
            status=TaskStatus.COMPLETED,
        )
        run = Run(
            id=new_id("ui_reply_run"),
            task_id=task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary=f"UI reply for {objective_id or project_id}",
        )
        from .skills import SkillInvocation, invoke_skill
        skill = self._skill_registry().get("ui_responder")
        invocation = SkillInvocation(
            skill_name="ui_responder",
            inputs={
                "operator_message": comment_text,
                "context_payload": {"prompt_envelope": prompt},
            },
            task=task,
            run=run,
            run_dir=run_dir,
        )
        skill_result = invoke_skill(skill, invocation, llm_router, telemetry=getattr(self.ctx, "telemetry", None))
        if not skill_result.success:
            return None
        parsed = skill_result.output
        return ResponderResult(
            reply=str(parsed.get("reply") or ""),
            recommended_action=str(parsed.get("recommended_action") or "none"),
            evidence_refs=list(parsed.get("evidence_refs") or []),
            mode_shift=str(parsed.get("mode_shift") or "none"),
            retrieved_memories=packet.retrieved_memories,
            llm_backend=skill_result.llm_backend or "",
            prompt_path=skill_result.prompt_path or "",
            response_path=skill_result.response_path or "",
        )

    def _interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        deterministic = self._deterministic_interrogation_review(objective_id)
        completions = self.store.list_context_records(objective_id=objective_id, record_type="interrogation_completed")
        latest_completion = completions[-1] if completions else None
        if latest_completion is not None:
            return self._recorded_interrogation_review(latest_completion, completed=True)

        drafts = self.store.list_context_records(objective_id=objective_id, record_type="interrogation_draft")
        latest_draft = drafts[-1] if drafts else None
        if latest_draft is not None:
            return self._recorded_interrogation_review(latest_draft, completed=False)

        if deterministic["plan_elements"]:
            generated = self._generate_interrogation_review(objective_id)
            if generated.get("generated_by") != "deterministic":
                self._persist_interrogation_record("interrogation_draft", objective, generated)
                drafts = self.store.list_context_records(objective_id=objective_id, record_type="interrogation_draft")
                latest_draft = drafts[-1] if drafts else None
                if latest_draft is not None:
                    return self._recorded_interrogation_review(latest_draft, completed=False)
        return deterministic

    def _generate_interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        deterministic = self._deterministic_interrogation_review(objective_id)
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is None:
            return deterministic

        intent_model = self.store.latest_intent_model(objective_id)
        comments = self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")[-6:]
        orchestrator = self._red_team_loop_orchestrator(llm_router)
        initial_inputs = {
            "objective_title": objective.title,
            "objective_summary": objective.summary,
            "intent_summary": intent_model.intent_summary if intent_model else "",
            "success_definition": intent_model.success_definition if intent_model else "",
            "non_negotiables": list(intent_model.non_negotiables) if intent_model else [],
            "recent_comments": [r.content for r in comments],
            "deterministic_review": deterministic,
        }

        def stopping_predicate(output, reviewer_results, round_number):
            if bool(output.get("ready_for_mermaid_review")):
                return True
            findings = list(output.get("red_team_findings") or [])
            return not findings

        loop_result = orchestrator.execute(
            generator_skill_name="interrogation",
            reviewer_skill_names=None,
            initial_inputs=initial_inputs,
            stopping_predicate=stopping_predicate,
            max_rounds=_INTERROGATION_RED_TEAM_MAX_ROUNDS,
            project_id=objective.project_id,
            loop_label="interrogation",
            loop_key=objective_id,
        )
        if not loop_result.success or not loop_result.final_output:
            return deterministic
        parsed = loop_result.final_output
        last_round = loop_result.history[-1] if loop_result.history else None
        return {
            "completed": False,
            "summary": str(parsed.get("summary") or ""),
            "plan_elements": list(parsed.get("plan_elements") or []),
            "questions": list(parsed.get("questions") or []),
            "generated_by": "llm",
            "backend": last_round.generator_result.llm_backend if last_round else "",
            "prompt_path": last_round.generator_result.prompt_path if last_round else "",
            "response_path": last_round.generator_result.response_path if last_round else "",
            "red_team_rounds": loop_result.rounds_completed,
            "red_team_stop_reason": loop_result.stop_reason,
        }

    def _deterministic_interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        intent_model = self.store.latest_intent_model(objective_id)
        desired_outcome = (intent_model.intent_summary if intent_model is not None else "").strip()
        success_definition = (intent_model.success_definition if intent_model is not None else "").strip()
        non_negotiables = list(intent_model.non_negotiables) if intent_model is not None else []

        plan_elements: list[str] = []
        if desired_outcome:
            plan_elements.append(f"Desired outcome: {desired_outcome}")
        if success_definition:
            plan_elements.append(f"Success definition: {success_definition}")
        if non_negotiables:
            plan_elements.append("Non-negotiables: " + ", ".join(non_negotiables[:4]))

        questions: list[str] = []
        if desired_outcome:
            questions.append("What concrete operator experience should feel different if this objective succeeds?")
        else:
            questions.append("What exact outcome should exist before the harness starts planning?")
        if success_definition:
            questions.append("What evidence would prove this objective is complete instead of only improved?")
        else:
            questions.append("How should the harness measure success for this objective?")
        questions.append("What is the most likely way the current plan could still miss your intent?")
        questions.append("What ambiguity should be resolved before Mermaid review?")
        return {
            "completed": False,
            "summary": "The harness should interrogate the objective and self-red-team the plan before Mermaid review.",
            "plan_elements": plan_elements,
            "questions": questions,
            "generated_by": "deterministic",
            "backend": None,
        }

    def _recorded_interrogation_review(self, record: ContextRecord, *, completed: bool) -> dict[str, object]:
        return {
            "completed": completed,
            "summary": record.content,
            "plan_elements": list(record.metadata.get("plan_elements") or []),
            "questions": list(record.metadata.get("questions") or []),
            "generated_by": record.metadata.get("generated_by", "deterministic"),
            "backend": record.metadata.get("backend"),
        }

    def _persist_interrogation_record(self, record_type: str, objective: Objective, review: dict[str, object]) -> None:
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type=record_type,
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="system",
                content=str(review["summary"]),
                metadata={
                    "plan_elements": review["plan_elements"],
                    "questions": review["questions"],
                    "generated_by": review.get("generated_by", "deterministic"),
                    "backend": review.get("backend"),
                    "prompt_path": review.get("prompt_path"),
                    "response_path": review.get("response_path"),
                },
            )
        )

    def _should_auto_complete_interrogation(self, objective_id: str) -> bool:
        review = self._interrogation_review(objective_id)
        if review.get("completed"):
            return False
        questions = list(review.get("questions") or [])
        if not questions:
            return False
        intent_model = self.store.latest_intent_model(objective_id)
        created_at = intent_model.created_at.isoformat() if intent_model is not None else ""
        answers = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")
            if not created_at or record.created_at.isoformat() >= created_at
        ]
        if len(answers) >= len(questions):
            return True
        return any(len((record.content or "").strip()) >= 48 for record in answers)

    def _build_interrogation_prompt(self, objective_id: str, deterministic: dict[str, object]) -> str:
        objective = self.store.get_objective(objective_id)
        intent_model = self.store.latest_intent_model(objective_id)
        comments = self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")[-6:]
        return (
            "You are red-teaming a software objective before process review.\n"
            "Your job is to interrogate the objective, extract the likely plan elements, and list the sharpest unresolved questions.\n"
            "Return JSON only with keys: summary, plan_elements, questions.\n"
            "summary: short paragraph\n"
            "plan_elements: array of concise strings\n"
            "questions: array of concise red-team questions\n\n"
            f"Objective title: {objective.title if objective else ''}\n"
            f"Objective summary: {objective.summary if objective else ''}\n"
            f"Intent summary: {intent_model.intent_summary if intent_model else ''}\n"
            f"Success definition: {intent_model.success_definition if intent_model else ''}\n"
            f"Non-negotiables: {json.dumps(intent_model.non_negotiables if intent_model else [])}\n"
            f"Recent operator comments: {json.dumps([record.content for record in comments], indent=2)}\n"
            f"Current deterministic review: {json.dumps(deterministic, indent=2, sort_keys=True)}\n"
        )

    def _parse_interrogation_response(self, text: str) -> dict[str, object] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        candidates.extend(fenced)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            summary = str(payload.get("summary") or "").strip()
            plan_elements = [str(item).strip() for item in list(payload.get("plan_elements") or []) if str(item).strip()]
            questions = [str(item).strip() for item in list(payload.get("questions") or []) if str(item).strip()]
            if summary and plan_elements and questions:
                return {
                    "summary": summary,
                    "plan_elements": plan_elements,
                    "questions": questions,
                }
        return None

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

    def _build_ui_responder_prompt(
        self,
        *,
        packet: ResponderContextPacket,
        project_id: str,
        objective_id: str | None,
        task_id: str | None,
        comment_text: str,
    ) -> str:
        project = self.store.get_project(project_id)
        objective = self.store.get_objective(objective_id) if objective_id else None
        intent_model = self.store.latest_intent_model(objective_id) if objective_id else None
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control") if objective_id else None
        interrogation_review = self._interrogation_review(objective_id) if objective_id else {}
        task = self.store.get_task(task_id) if task_id else None
        run = None
        if task is not None:
            task_runs = self.store.list_runs(task.id)
            run = task_runs[-1] if task_runs else None
        else:
            task, run = self._latest_linked_task_and_run(project_id=project_id, objective_id=objective_id)
        run_output = self.run_cli_output(run.id) if run is not None else {}
        task_insight = self.task_failure_insight(task.id) if task is not None else {}
        all_records = self.store.list_context_records(objective_id=objective_id) if objective_id else self.store.list_context_records(project_id=project_id)
        context_records = [
            {
                "record_type": record.record_type,
                "created_at": record.created_at.isoformat(),
                "author_type": record.author_type,
                "author_id": record.author_id,
                "visibility": record.visibility,
                "task_id": record.task_id,
                "run_id": record.run_id,
                "content": record.content,
                "metadata": record.metadata,
            }
            for record in all_records
        ]
        payload = {
            "project": serialize_dataclass(project) if project is not None else None,
            "mode": packet.mode,
            "next_action": {
                "title": packet.next_action_title,
                "body": packet.next_action_body,
            },
            "objective": serialize_dataclass(objective) if objective is not None else None,
            "intent_model": serialize_dataclass(intent_model) if intent_model is not None else None,
            "interrogation_review": interrogation_review,
            "mermaid": (
                {
                    "status": mermaid.status.value,
                    "summary": mermaid.summary,
                    "content": mermaid.content,
                    "version": mermaid.version,
                    "blocking_reason": mermaid.blocking_reason,
                }
                if mermaid is not None
                else None
            ),
            "latest_task": serialize_dataclass(task) if task is not None else None,
            "selected_task_insight": task_insight if task is not None else None,
            "latest_run": serialize_dataclass(run) if run is not None else None,
            "latest_run_output": run_output,
            "recent_turns": [serialize_dataclass(turn) for turn in packet.recent_turns],
            "retrieved_memories": [serialize_dataclass(memory) for memory in packet.retrieved_memories],
            "frustration_detected": packet.frustration_detected,
            "all_context_records": context_records,
            "operator_message": comment_text,
        }
        return (
            "You are the accrivia-harness UI responder.\n"
            "Answer the operator's latest message directly and concretely.\n"
            "Use the full current objective context, not just the latest run.\n"
            "Do not dodge the question. Do not default to boilerplate about reviewing output unless that directly answers the question.\n"
            "If the operator asks where red-team belongs, answer that directly from the planning/control-flow context.\n"
            "Prefer plain language and explain what stage the operator is in when relevant.\n"
            "Return JSON only with keys: reply, recommended_action, evidence_refs, mode_shift.\n"
            "reply: short plain-language answer to the operator\n"
            "recommended_action: one of none, answer_prompt, review_mermaid, review_run, start_run, open_investigation\n"
            "evidence_refs: array of short strings\n"
            "mode_shift: one of none, investigation\n\n"
            f"Context:\n{json.dumps(payload, indent=2, sort_keys=True)}\n"
        )

    def _parse_ui_responder_response(self, text: str) -> dict[str, object] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        candidates.extend(fenced)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            reply = str(payload.get("reply") or "").strip()
            if not reply:
                continue
            recommended_action = str(payload.get("recommended_action") or "none").strip() or "none"
            mode_shift = str(payload.get("mode_shift") or "none").strip() or "none"
            evidence_refs = [
                str(item).strip()
                for item in list(payload.get("evidence_refs") or [])
                if str(item).strip()
            ]
            return {
                "reply": reply,
                "recommended_action": recommended_action,
                "mode_shift": mode_shift,
                "evidence_refs": evidence_refs,
            }
        return None

    def _generate_mermaid_update_proposal(self, objective_id: str, *, directive: str) -> dict[str, str] | None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return None
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is None or not getattr(llm_router, "executors", {}):
            return None
        intent_model = self.store.latest_intent_model(objective_id)
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        comments = self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")[-12:]
        anchor_match = re.search(r"\[Mermaid anchor:\s*([^\]]+)\]", directive)
        anchor_label = anchor_match.group(1).strip() if anchor_match else ""
        rewrite_requested = bool(
            re.search(r"\b(rewrite|regenerate|redo|rebuild|start over|restructure|replace the diagram|full rewrite)\b", directive, flags=re.IGNORECASE)
        )
        orchestrator = self._red_team_loop_orchestrator(llm_router)
        initial_inputs = {
            "objective_title": objective.title,
            "objective_summary": objective.summary,
            "intent_summary": intent_model.intent_summary if intent_model else "",
            "success_definition": intent_model.success_definition if intent_model else "",
            "non_negotiables": list(intent_model.non_negotiables) if intent_model else [],
            "current_mermaid": mermaid.content if mermaid else "",
            "directive": directive,
            "anchor_label": anchor_label,
            "rewrite_requested": rewrite_requested,
            "recent_comments": [r.content for r in comments],
        }
        latest_review_box: dict[str, object] = {"review": None}

        def run_mermaid_review(proposed_text: str) -> dict[str, object]:
            try:
                return interrogation_service.red_team_mermaid_text(
                    proposed_text,
                    source_label=f"mermaid_proposal_{objective_id}",
                    include_llm=False,
                )
            except Exception as exc:  # noqa: BLE001
                return {
                    "ready_for_human_review": False,
                    "deterministic_review": {"findings": [
                        {"severity": "critical", "message": f"mermaid review failed: {exc}"}
                    ]},
                    "llm_review": {"findings": []},
                }

        def stopping_predicate(output, reviewer_results, round_number):
            proposed = str(output.get("proposed_content") or "")
            if not proposed:
                return True  # bail — nothing to review, let loop record failure
            review = run_mermaid_review(proposed)
            latest_review_box["review"] = review
            deterministic_findings = list((review.get("deterministic_review") or {}).get("findings") or [])
            major = [
                f for f in deterministic_findings
                if str(f.get("severity") or "").lower() in {"critical", "major"}
            ]
            return bool(review.get("ready_for_human_review")) and not major

        def findings_extractor(generator_output, reviewer_results):
            review = latest_review_box.get("review") or {}
            findings: list[str] = []
            for item in list((review.get("deterministic_review") or {}).get("findings") or []):
                summary = str(item.get("summary") or item.get("message") or item.get("finding") or "").strip()
                patch_hint = str(item.get("patch_hint") or "").strip()
                severity = str(item.get("severity") or "info").strip()
                if summary or patch_hint:
                    line = f"[deterministic:{severity}] {summary}"
                    if patch_hint:
                        line += f" — fix: {patch_hint}"
                    findings.append(line)
            return findings

        loop_result = orchestrator.execute(
            generator_skill_name="mermaid_update_proposal",
            reviewer_skill_names=None,
            initial_inputs=initial_inputs,
            stopping_predicate=stopping_predicate,
            max_rounds=_MERMAID_RED_TEAM_MAX_ROUNDS,
            project_id=objective.project_id,
            loop_label="mermaid_update_proposal",
            loop_key=objective.id,
            findings_extractor=findings_extractor,
        )
        if not loop_result.success or not loop_result.final_output:
            return None
        proposed_content = str(loop_result.final_output.get("proposed_content") or "")
        rationale = str(loop_result.final_output.get("rationale") or "")
        if not proposed_content:
            return None
        last_round = loop_result.history[-1] if loop_result.history else None
        last_review = latest_review_box.get("review") or {}
        return {
            "summary": rationale,
            "content": proposed_content,
            "backend": last_round.generator_result.llm_backend if last_round else "",
            "prompt_path": last_round.generator_result.prompt_path if last_round else "",
            "response_path": last_round.generator_result.response_path if last_round else "",
            "red_team_rounds": loop_result.rounds_completed,
            "red_team_stop_reason": loop_result.stop_reason,
            "red_team_review": json.dumps(last_review, indent=2, sort_keys=True),
        }

    def _parse_mermaid_update_response(self, text: str) -> dict[str, str] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        candidates.extend(fenced)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            summary = str(payload.get("summary") or "").strip()
            content = str(payload.get("content") or "").strip()
            if summary and content:
                return {"summary": summary, "content": content}
        return None

    def _proposal_record(self, objective_id: str, proposal_id: str) -> ContextRecord | None:
        for record in self.store.list_context_records(objective_id=objective_id, record_type="mermaid_update_proposed"):
            if record.id == proposal_id:
                return record
        return None

    def _latest_mermaid_proposal(self, objective_id: str) -> dict[str, object] | None:
        proposals = self.store.list_context_records(objective_id=objective_id, record_type="mermaid_update_proposed")
        if not proposals:
            return None
        resolutions = {
            str(record.metadata.get("proposal_id") or "")
            for record in self.store.list_context_records(objective_id=objective_id)
            if record.record_type in {"mermaid_update_accepted", "mermaid_update_rejected", "mermaid_update_rewound"}
        }
        proposal = proposals[-1]
        if proposal.id in resolutions:
            return None
        return {
            "id": proposal.id,
            "summary": proposal.content,
            "content": str(proposal.metadata.get("content") or ""),
            "directive": str(proposal.metadata.get("directive") or ""),
            "backend": str(proposal.metadata.get("backend") or ""),
            "created_at": proposal.created_at.isoformat(),
        }

    def _atomic_generation_state(self, objective_id: str) -> dict[str, object]:
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        diagram_version = mermaid.version if mermaid is not None else None
        starts = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_started")
            if diagram_version is None or int(record.metadata.get("diagram_version") or 0) == diagram_version
        ]
        if not starts:
            return {
                "status": "idle",
                "diagram_version": diagram_version,
                "generation_id": "",
                "started_at": "",
                "completed_at": "",
                "failed_at": "",
                "unit_count": 0,
            }
        start = starts[-1]
        generation_id = str(start.metadata.get("generation_id") or start.id)
        completed = next(
            (
                record
                for record in reversed(self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_completed"))
                if str(record.metadata.get("generation_id") or "") == generation_id
            ),
            None,
        )
        failed = next(
            (
                record
                for record in reversed(self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_failed"))
                if str(record.metadata.get("generation_id") or "") == generation_id
            ),
            None,
        )
        unit_count = len(
            [
                record
                for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_unit_generated")
                if str(record.metadata.get("generation_id") or "") == generation_id
            ]
        )
        progress = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_progress")
            if str(record.metadata.get("generation_id") or "") == generation_id
        ]
        status = "running"
        if failed is not None:
            status = "failed"
        elif completed is not None:
            status = "completed"
        phase = ""
        if status == "completed":
            phase = "complete"
        elif status == "failed":
            phase = "failed"
        elif progress:
            phase = str(progress[-1].metadata.get("phase") or "")
        related_times = [start.created_at]
        if progress:
            related_times.extend(record.created_at for record in progress)
        related_times.extend(
            record.created_at
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_unit_generated")
            if str(record.metadata.get("generation_id") or "") == generation_id
        )
        if completed is not None:
            related_times.append(completed.created_at)
        if failed is not None:
            related_times.append(failed.created_at)
        last_activity_at = max(related_times).isoformat() if related_times else ""
        # Extract refinement round and latest critique/coverage from telemetry
        telemetry = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_decomposition_telemetry")
            if str(record.metadata.get("generation_id") or "") == generation_id
        ]
        atomic_phases = self.workflow_timing.sequential_phase_rows(
            start.created_at,
            [(str(record.metadata.get("phase") or ""), record.created_at) for record in progress],
            completed_at=completed.created_at if completed is not None else None,
            failed_at=failed.created_at if failed is not None else None,
            last_activity_at=max(related_times) if related_times else None,
        )
        round_map: dict[int, dict[str, object]] = {}
        for record in telemetry:
            raw_round = record.metadata.get("round")
            if raw_round in (None, ""):
                continue
            try:
                round_number = int(raw_round)
            except (TypeError, ValueError):
                continue
            event_type = str(record.metadata.get("event_type") or "")
            current = round_map.setdefault(
                round_number,
                {
                    "round_number": round_number,
                    "started_at": record.created_at.isoformat(),
                    "ended_at": record.created_at.isoformat(),
                    "duration_ms": 0,
                    "events": [],
                    "critique_accepted": None,
                    "coverage_accepted": None,
                    "stalled": False,
                    "unit_count": 0,
                },
            )
            current["ended_at"] = max(str(current.get("ended_at") or ""), record.created_at.isoformat())
            current_events = list(current.get("events") or [])
            current_events.append(event_type)
            current["events"] = current_events
            if event_type == "round_complete":
                current["duration_ms"] = max(
                    int(current.get("duration_ms") or 0),
                    int(float(record.metadata.get("total_round_seconds") or 0.0) * 1000),
                )
                current["critique_accepted"] = record.metadata.get("critique_accepted")
                current["coverage_accepted"] = record.metadata.get("coverage_accepted")
                current["unit_count"] = int(record.metadata.get("unit_count") or current.get("unit_count") or 0)
            elif event_type == "critique":
                current["critique_accepted"] = record.metadata.get("accepted")
                current["unit_count"] = int(record.metadata.get("unit_count") or current.get("unit_count") or 0)
            elif event_type == "coverage":
                current["coverage_accepted"] = record.metadata.get("complete")
                current["unit_count"] = int(record.metadata.get("unit_count") or current.get("unit_count") or 0)
            elif event_type in {"generate", "refine"}:
                current["unit_count"] = int(record.metadata.get("unit_count") or record.metadata.get("unit_count_after") or current.get("unit_count") or 0)
            elif event_type in {"stall_detected", "stall_exit"}:
                current["stalled"] = True
        atomic_rounds = []
        for round_number in sorted(round_map):
            current = round_map[round_number]
            if not int(current.get("duration_ms") or 0):
                current["duration_ms"] = self.workflow_timing.duration_ms(
                    str(current.get("started_at") or ""),
                    last_activity_at=str(current.get("ended_at") or ""),
                )
            atomic_rounds.append(current)
        refinement_round = 0
        critique_accepted = None
        coverage_complete = None
        last_critique_problems = []
        last_coverage_gaps = []
        for record in telemetry:
            evt = record.metadata.get("event_type", "")
            rnd = record.metadata.get("round")
            if rnd is not None and int(rnd) > refinement_round:
                refinement_round = int(rnd)
            if evt == "critique":
                critique_accepted = record.metadata.get("accepted")
                last_critique_problems = list(record.metadata.get("problems") or [])
            if evt == "coverage":
                coverage_complete = record.metadata.get("complete")
                last_coverage_gaps = list(record.metadata.get("gaps") or [])
        return {
            "status": status,
            "diagram_version": diagram_version,
            "generation_id": generation_id,
            "started_at": start.created_at.isoformat(),
            "completed_at": completed.created_at.isoformat() if completed is not None else "",
            "failed_at": failed.created_at.isoformat() if failed is not None else "",
            "unit_count": unit_count,
            "phase": phase,
            "last_activity_at": last_activity_at,
            "duration_ms": self.workflow_timing.duration_ms(
                start.created_at,
                completed_at=completed.created_at if completed is not None else None,
                failed_at=failed.created_at if failed is not None else None,
                last_activity_at=max(related_times) if related_times else None,
            ),
            "atomic_phases": atomic_phases,
            "atomic_rounds": atomic_rounds,
            "error": failed.content if failed is not None else "",
            "refinement_round": refinement_round,
            "critique_accepted": critique_accepted,
            "coverage_complete": coverage_complete,
            "last_critique_problems": last_critique_problems,
            "last_coverage_gaps": last_coverage_gaps,
            "is_stale": self._atomic_generation_is_stale(
                {
                    "status": status,
                    "last_activity_at": last_activity_at,
                },
                objective_id,
            ),
        }

    def _atomic_units_for_objective(
        self,
        objective_id: str,
        linked_tasks: list[Task],
        generation_state: dict[str, object],
    ) -> list[dict[str, object]]:
        generation_id = str(generation_state.get("generation_id") or "")
        if not generation_id:
            return []
        tasks_by_id = {task.id: task for task in linked_tasks}
        task_runs = {task.id: self.store.list_runs(task.id) for task in linked_tasks}
        units: list[dict[str, object]] = []
        records = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_unit_generated")
            if str(record.metadata.get("generation_id") or "") == generation_id
        ]
        published_task_ids: set[str] = set()

        for record in records:
            task_id = str(record.metadata.get("task_id") or "")
            if task_id:
                published_task_ids.add(task_id)
            task = tasks_by_id.get(task_id)
            runs = task_runs.get(task_id, [])
            latest_run = runs[-1] if runs else None

            status = task.status.value if task is not None else "pending"

            # Read validation results from the report artifact if available.
            validation_info = None
            if latest_run is not None:
                report_artifacts = [
                    a for a in self.store.list_artifacts(latest_run.id)
                    if a.kind == "report" and a.path
                ]
                if report_artifacts:
                    try:
                        import json as _json
                        report_data = _json.loads(Path(report_artifacts[-1].path).read_text(encoding="utf-8"))
                        cc = report_data.get("compile_check")
                        tc = report_data.get("test_check")
                        if cc is not None or tc is not None:
                            validation_info = {
                                "compile_passed": bool(cc.get("passed")) if cc else None,
                                "test_passed": bool(tc.get("passed")) if tc else None,
                                "test_timed_out": bool(tc.get("timed_out")) if tc else False,
                            }
                    except Exception:
                        pass

            units.append(
                {
                    "id": task_id or record.id,
                    "title": str(record.metadata.get("title") or (task.title if task else record.content)),
                    "objective": str(record.metadata.get("objective") or (task.objective if task else "")),
                    "rationale": str(record.metadata.get("rationale") or ""),
                    "strategy": str(record.metadata.get("strategy") or (task.strategy if task else "")),
                    "status": status,
                    "order": int(record.metadata.get("order") or 0),
                    "published_unit": True,
                    "latest_run": (
                        {
                            "attempt": latest_run.attempt,
                            "status": latest_run.status.value,
                            "started_at": latest_run.created_at.isoformat() if latest_run.created_at else None,
                            "finished_at": latest_run.updated_at.isoformat() if latest_run.status.value in ("completed", "failed", "blocked", "disposed") and latest_run.updated_at else None,
                            "validation": validation_info,
                        }
                        if latest_run is not None
                        else None
                    ),
                }
            )
        next_order = len(units) + 1
        for task in linked_tasks:
            if task.id in published_task_ids:
                continue
            runs = task_runs.get(task.id, [])
            latest_run = runs[-1] if runs else None
            validation_info = None
            if latest_run is not None:
                report_artifacts = [
                    a for a in self.store.list_artifacts(latest_run.id)
                    if a.kind == "report" and a.path
                ]
                if report_artifacts:
                    try:
                        import json as _json
                        report_data = _json.loads(Path(report_artifacts[-1].path).read_text(encoding="utf-8"))
                        cc = report_data.get("compile_check")
                        tc = report_data.get("test_check")
                        if cc is not None or tc is not None:
                            validation_info = {
                                "compile_passed": bool(cc.get("passed")) if cc else None,
                                "test_passed": bool(tc.get("passed")) if tc else None,
                                "test_timed_out": bool(tc.get("timed_out")) if tc else False,
                            }
                    except Exception:
                        pass
            units.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "objective": task.objective,
                    "rationale": "",
                    "strategy": task.strategy,
                    "status": task.status.value,
                    "order": next_order,
                    "published_unit": False,
                    "latest_run": (
                        {
                            "attempt": latest_run.attempt,
                            "status": latest_run.status.value,
                            "started_at": latest_run.created_at.isoformat() if latest_run.created_at else None,
                            "finished_at": latest_run.updated_at.isoformat() if latest_run.status.value in ("completed", "failed", "blocked", "disposed") and latest_run.updated_at else None,
                            "validation": validation_info,
                        }
                        if latest_run is not None
                        else None
                    ),
                }
            )
            next_order += 1
        return sorted(units, key=lambda item: (int(item["order"]), str(item["title"])))

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

    def _build_responder_context_packet(
        self,
        *,
        project_id: str,
        objective_id: str | None,
        task_id: str | None,
        comment_text: str,
        frustration_detected: bool,
    ) -> ResponderContextPacket:
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        objective = self.store.get_objective(objective_id) if objective_id else None
        intent_model = self.store.latest_intent_model(objective_id) if objective_id else None
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control") if objective_id else None
        next_action = self._next_action_for_context(objective_id)
        task = self.store.get_task(task_id) if task_id else None
        if task is not None and task.project_id != project_id:
            raise ValueError(f"Unknown task for project: {task_id}")
        run = None
        if task is not None:
            task_runs = self.store.list_runs(task.id)
            run = task_runs[-1] if task_runs else None
        else:
            task, run = self._latest_linked_task_and_run(project_id=project_id, objective_id=objective_id)
        run_context = None
        if run is not None:
            run_context = _AttrDict(
                run_id=run.id,
                attempt=run.attempt,
                status=run.status.value,
                summary=(run.summary or "").strip(),
                available_sections=[section.label for section in self._run_output_sections(run.id)],
                section_previews={
                    section.label: self._truncate_text(section.content, 220)
                    for section in self._run_output_sections(run.id)
                },
            )
        task_context = None
        if task is not None:
            insight = self.task_failure_insight(task.id)
            task_context = _AttrDict(
                task_id=task.id,
                title=task.title,
                status=task.status.value,
                strategy=task.strategy,
                objective=task.objective,
                analysis_summary=str(insight.get("analysis_summary") or ""),
                failure_message=str(insight.get("failure_message") or ""),
                root_cause_hint=str(insight.get("root_cause_hint") or ""),
                backend_failure_kind=str(insight.get("backend_failure_kind") or ""),
                backend_failure_explanation=str(insight.get("backend_failure_explanation") or ""),
                evidence_to_inspect=[str(item) for item in list(insight.get("suggested_evidence") or []) if str(item)],
            )
        objective_context = None
        if objective is not None:
            objective_context = _AttrDict(
                objective_id=objective.id,
                title=objective.title,
                status=objective.status.value,
                summary=objective.summary,
                intent_summary=(intent_model.intent_summary if intent_model is not None else ""),
                success_definition=(intent_model.success_definition if intent_model is not None else ""),
                non_negotiables=(intent_model.non_negotiables if intent_model is not None else []),
                mermaid_status=(mermaid.status.value if mermaid is not None else ""),
                mermaid_summary=(mermaid.summary if mermaid is not None else ""),
            )
        retrieved_memories = []
        if self.memory_provider is not None:
            retrieved_memories = self.memory_provider.retrieve(
                project_id=project.id,
                objective_id=objective_id,
                query_text=comment_text,
                limit=4,
            )
        current_mode = "empty"
        interrogation_question = ""
        interrogation_remaining = 0
        if objective is not None:
            current_mode = self._focus_mode_for_objective(objective.id)
            if current_mode == "interrogation_review":
                review = self._interrogation_review(objective.id)
                questions = list(review.get("questions") or [])
                intent_created_at = intent_model.created_at.isoformat() if intent_model is not None else ""
                relevant_answers = [
                    record
                    for record in self.store.list_context_records(objective_id=objective.id, record_type="operator_comment")
                    if not intent_created_at or record.created_at.isoformat() >= intent_created_at
                ]
                question_index = min(len(relevant_answers), max(0, len(questions) - 1))
                if questions:
                    interrogation_question = questions[question_index]
                    interrogation_remaining = max(0, len(questions) - question_index - 1)
        return ResponderContextPacket(
            project_id=project.id,
            project_name=project.name,
            mode=current_mode,
            next_action_title=next_action["title"],
            next_action_body=next_action["body"],
            objective=objective_context,
            task=task_context,
            run=run_context,
            recent_turns=self._recent_conversation_turns(project_id=project_id, objective_id=objective_id, task_id=task_id),
            frustration_detected=frustration_detected,
            retrieved_memories=retrieved_memories,
            interrogation_question=interrogation_question,
            interrogation_remaining=interrogation_remaining,
        )

    def _log_ui_memory_retrieval(
        self,
        *,
        project_id: str,
        objective_id: str | None,
        task_id: str | None,
        comment_text: str,
        responder_result: ResponderResult,
    ) -> None:
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="ui_memory_retrieval",
                project_id=project_id,
                objective_id=objective_id,
                task_id=task_id,
                visibility="system_only",
                author_type="system",
                content=comment_text,
                metadata={
                    "retrieved_count": len(responder_result.retrieved_memories),
                    "retrieved_memories": [serialize_dataclass(memory) for memory in responder_result.retrieved_memories],
                    "recommended_action": responder_result.recommended_action,
                    "mode_shift": responder_result.mode_shift,
                    "evidence_refs": responder_result.evidence_refs,
                },
            )
        )

    def _recent_conversation_turns(self, *, project_id: str, objective_id: str | None, task_id: str | None = None) -> list[ConversationTurn]:
        turns: list[ConversationTurn] = []
        for record_type, role in (("operator_comment", "operator"), ("harness_reply", "harness")):
            for record in self.store.list_context_records(
                project_id=project_id,
                objective_id=objective_id,
                task_id=task_id,
                record_type=record_type,
            ):
                turns.append(
                    ConversationTurn(
                        role=role,
                        text=record.content,
                        created_at=record.created_at.isoformat(),
                    )
                )
        turns.sort(key=lambda item: item["created_at"])
        return turns[-10:]

    def task_conversation(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        project = self.store.get_project(task.project_id)
        if project is None:
            raise ValueError(f"Unknown project for task: {task_id}")
        objective = self.store.get_objective(task.objective_id) if task.objective_id else None
        task_records = self.store.list_context_records(project_id=project.id, objective_id=task.objective_id, task_id=task.id)
        comment_records = [record for record in task_records if record.record_type == "operator_comment"]
        reply_records = [
            record
            for record in task_records
            if record.record_type in {"harness_reply_pending", "harness_reply", "harness_reply_failed"}
        ]
        replies_by_comment: dict[str, list[ContextRecord]] = {}
        for record in reply_records:
            reply_to = str(record.metadata.get("reply_to") or "")
            if not reply_to:
                continue
            replies_by_comment.setdefault(reply_to, []).append(record)
        turns: list[dict[str, object]] = []
        rank = {"harness_reply_pending": 0, "harness_reply_failed": 1, "harness_reply": 2}
        now = _dt.datetime.now(_dt.timezone.utc)
        for comment in comment_records:
            turns.append(
                {
                    "id": comment.id,
                    "role": "operator",
                    "text": comment.content,
                    "created_at": comment.created_at.isoformat(),
                    "status": "completed",
                }
            )
            candidates = replies_by_comment.get(comment.id, [])
            if not candidates:
                continue
            selected = sorted(
                candidates,
                key=lambda record: (rank.get(record.record_type, -1), record.created_at.isoformat()),
            )[-1]
            queued_at_raw = selected.metadata.get("queued_at")
            started_at_raw = selected.metadata.get("started_at")
            completed_at_raw = selected.metadata.get("completed_at")
            status = str(selected.metadata.get("status") or "")
            stale = False
            stale_elapsed_ms: int | None = None
            if selected.record_type == "harness_reply_pending":
                anchor_raw = str(queued_at_raw or selected.created_at.isoformat() or "")
                try:
                    anchor_dt = _dt.datetime.fromisoformat(anchor_raw)
                    stale_elapsed_ms = max(0, int((now - anchor_dt).total_seconds() * 1000))
                    stale = stale_elapsed_ms >= (_TASK_REPLY_STALE_SECONDS * 1000)
                except ValueError:
                    anchor_dt = None
                if stale:
                    status = "failed"
            turns.append(
                {
                    "id": selected.id,
                    "role": "harness",
                    "text": (
                        f"{selected.content} Reply appears stalled and should be retried."
                        if stale
                        else selected.content
                    ),
                    "created_at": selected.created_at.isoformat(),
                    "status": status or ("pending" if selected.record_type == "harness_reply_pending" else "failed" if selected.record_type == "harness_reply_failed" else "completed"),
                    "pending": selected.record_type == "harness_reply_pending" and not stale,
                    "failed": selected.record_type == "harness_reply_failed" or stale,
                    "job_id": selected.metadata.get("job_id"),
                    "queued_at": queued_at_raw,
                    "started_at": started_at_raw,
                    "completed_at": completed_at_raw,
                    "elapsed_ms": selected.metadata.get("elapsed_ms") if not stale else stale_elapsed_ms,
                    "queue_wait_ms": selected.metadata.get("queue_wait_ms"),
                    "stale": stale,
                }
            )
        return {
            "task": serialize_dataclass(task),
            "objective": serialize_dataclass(objective) if objective is not None else None,
            "project": serialize_dataclass(project),
            "turns": turns[-20:],
        }

    def task_failure_insight(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        project = self.store.get_project(task.project_id)
        objective = self.store.get_objective(task.objective_id) if task.objective_id else None
        runs = self.store.list_runs(task.id)
        run = runs[-1] if runs else None
        evaluation = None
        sections_raw: list[RunOutputSection] = []
        summarized_run: dict[str, object] = {}
        if run is not None:
            evaluations = self.store.list_evaluations(run.id)
            evaluation = evaluations[-1] if evaluations else None
            sections_raw = self._run_output_sections(run.id)
            summarized_run = self._summarize_run_output(run, sections_raw)
        diagnostics = evaluation.details.get("diagnostics") if evaluation is not None and isinstance(evaluation.details, dict) else {}
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        failure_message = str(
            diagnostics.get("failure_message")
            or diagnostics.get("error")
            or diagnostics.get("blocked_reason")
            or ""
        ).strip()
        root_cause_hint = str((evaluation.details if evaluation is not None else {}).get("root_cause_hint") or "").strip() if evaluation is not None else ""
        relevant_section_previews = self._task_failure_section_previews(sections_raw)
        normalized_failure = self._normalize_task_failure(
            failure_message=failure_message,
            root_cause_hint=root_cause_hint,
            section_previews=relevant_section_previews,
        )
        return {
            "project": serialize_dataclass(project) if project is not None else None,
            "objective": serialize_dataclass(objective) if objective is not None else None,
            "task": serialize_dataclass(task),
            "run": serialize_dataclass(run) if run is not None else None,
            "analysis_summary": str(evaluation.summary or "") if evaluation is not None else "",
            "failure_message": failure_message,
            "root_cause_hint": root_cause_hint,
            "failure_category": str(diagnostics.get("failure_category") or "").strip(),
            "run_summary": summarized_run,
            "available_sections": [section.label for section in sections_raw],
            "relevant_section_previews": relevant_section_previews,
            "backend_failure_kind": normalized_failure["kind"],
            "backend_failure_explanation": normalized_failure["explanation"],
            "suggested_evidence": normalized_failure["suggested_evidence"],
        }

    def _task_failure_section_previews(self, sections: list[RunOutputSection]) -> dict[str, str]:
        previews: dict[str, str] = {}
        for label in ("worker stderr", "codex worker stderr", "llm stderr", "report", "plan", "workspace metadata"):
            matching = next((section for section in sections if section.label == label), None)
            if matching is not None:
                previews[label] = self._truncate_text(matching.content, 220)
        return previews

    def _normalize_task_failure(
        self,
        *,
        failure_message: str,
        root_cause_hint: str,
        section_previews: dict[str, str],
    ) -> dict[str, object]:
        combined = "\n".join(
            part for part in [
                failure_message.strip(),
                root_cause_hint.strip(),
                *(section_previews.values()),
            ] if part
        ).lower()
        suggested_evidence = [label for label in ("worker stderr", "codex worker stderr", "llm stderr", "report", "plan", "workspace metadata") if label in section_previews]
        if "hit your limit" in combined or "quota" in combined or "credits exhausted" in combined or "out of credits" in combined:
            return {
                "kind": "quota",
                "explanation": "The failure looks like provider quota or credit exhaustion rather than a code defect.",
                "suggested_evidence": suggested_evidence or ["llm stderr", "worker stderr", "report"],
            }
        if "unauthorized" in combined or "incorrect username or password" in combined or "authentication" in combined or "api key" in combined or "login" in combined:
            return {
                "kind": "auth",
                "explanation": "The failure looks like an authentication or credential problem in the backend toolchain.",
                "suggested_evidence": suggested_evidence or ["worker stderr", "llm stderr", "report"],
            }
        if "all worker backends failed" in combined or "executor/infrastructure" in combined or "executor failed" in combined or "backend unavailable" in combined:
            return {
                "kind": "backend_unavailable",
                "explanation": "The failure looks like backend or executor infrastructure trouble, not a completed product-level judgment.",
                "suggested_evidence": suggested_evidence or ["worker stderr", "llm stderr", "report", "plan"],
            }
        return {
            "kind": "",
            "explanation": "",
            "suggested_evidence": suggested_evidence,
        }

    def _latest_linked_task_and_run(self, *, project_id: str, objective_id: str | None):
        linked_tasks = [
            task
            for task in self.store.list_tasks(project_id)
            if objective_id and task.objective_id == objective_id
        ]
        if not linked_tasks:
            return None, None
        best_pair = None
        for candidate in linked_tasks:
            candidate_runs = self.store.list_runs(candidate.id)
            candidate_latest_run = candidate_runs[-1] if candidate_runs else None
            candidate_sort_key = (
                candidate_latest_run.created_at if candidate_latest_run is not None else candidate.updated_at,
                candidate.id,
            )
            if best_pair is None or candidate_sort_key > best_pair[0]:
                best_pair = (candidate_sort_key, candidate, candidate_latest_run)
        assert best_pair is not None
        return best_pair[1], best_pair[2]

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

    def _create_seed_mermaid(self, objective: Objective) -> MermaidArtifact:
        artifact = MermaidArtifact(
            id=new_id("diagram"),
            objective_id=objective.id,
            diagram_type="workflow_control",
            version=self.store.next_mermaid_version(objective.id, "workflow_control"),
            status=MermaidStatus.DRAFT,
            summary="Initial workflow draft",
            content=self._default_objective_mermaid(objective),
            required_for_execution=True,
            blocking_reason="Workflow review has not been completed yet.",
            author_type="system",
        )
        self.store.create_mermaid_artifact(artifact)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_seeded",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="system",
                content="Seeded initial required Mermaid workflow.",
                metadata={
                    "diagram_id": artifact.id,
                    "diagram_type": artifact.diagram_type,
                    "version": artifact.version,
                    "status": artifact.status.value,
                },
            )
        )
        return artifact

    def _project_mermaid(self, project_id: str, tasks, runs_by_task: dict[str, list[Any]]) -> str:
        project = self.store.get_project(project_id)
        title = project.name if project is not None else project_id
        lines = ["flowchart TD", f'    P["Project: {self._mermaid_label(title)}"]']
        sorted_tasks = sorted(tasks, key=lambda item: (item.created_at, item.priority, item.id))
        latest_run_ids: list[str] = []
        for index, task in enumerate(sorted_tasks, start=1):
            task_node = f"T{index}"
            task_label = f"Task: {task.title}\\n{task.status.value} · {task.strategy}"
            lines.append(f'    {task_node}["{self._mermaid_label(task_label)}"]')
            if task.parent_task_id:
                parent_index = next(
                    (i for i, candidate in enumerate(sorted_tasks, start=1) if candidate.id == task.parent_task_id),
                    None,
                )
                if parent_index is not None:
                    lines.append(f"    T{parent_index} --> {task_node}")
                else:
                    lines.append(f"    P --> {task_node}")
            else:
                lines.append(f"    P --> {task_node}")
            runs = runs_by_task.get(task.id, [])
            if runs:
                latest_run = runs[-1]
                latest_run_ids.append(latest_run.id)
                run_node = f"R{index}"
                run_label = f"Run {latest_run.attempt}\\n{latest_run.status.value}"
                lines.append(f'    {run_node}["{self._mermaid_label(run_label)}"]')
                lines.append(f"    {task_node} --> {run_node}")
        if not sorted_tasks:
            lines.append('    P --> I["No tasks yet"]')
        return "\n".join(lines)

    def _default_objective_mermaid(self, objective: Objective) -> str:
        """Generate an objective decomposition diagram from the plan set.

        Delegates to `mermaid.render_mermaid_from_plans`, the single canonical
        renderer. Plans are the source of truth; node IDs are `P_<plan_hash>`
        (stable across revisions). Falls back to the "awaiting decomposition"
        placeholder when no plans exist yet.

        The previous implementation rendered from tasks using
        `_mermaid_node_id_for_task(task.id)` which produced `T_<task_suffix>`
        IDs. That path is removed — task IDs and plan IDs are no longer
        conflated. See Query #3 findings + the canonical ID design notes.
        """
        from .mermaid import render_mermaid_from_plans
        plans = self.store.list_plans_for_objective(objective.id)
        return render_mermaid_from_plans(plans, objective)

    @staticmethod
    def _mermaid_label(value: str) -> str:
        return value.replace('"', "'")

    @staticmethod
    def _comment_looks_like_frustration(text: str) -> bool:
        lowered = text.lower()
        triggers = [
            "frustrat",
            "annoy",
            "confus",
            "stuck",
            "what am i supposed",
            "doesn't make sense",
            "terrible",
            "bad ux",
        ]
        return any(trigger in lowered for trigger in triggers)

    def _comment_requests_mermaid_update(
        self,
        text: str,
        *,
        project_id: str,
        objective_id: str | None,
    ) -> bool:
        lowered = text.lower().strip()
        mermaid_terms = ("mermaid", "diagram", "control flow", "flowchart", "flow chart")
        update_terms = ("update", "revise", "regenerate", "rewrite", "reflect this", "change", "remove", "add", "fix")
        if any(term in lowered for term in mermaid_terms) and any(term in lowered for term in update_terms):
            return True
        latest_mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control") if objective_id else None
        proposal_pending = self._latest_mermaid_proposal(objective_id) is not None if objective_id else False
        in_mermaid_review = latest_mermaid is not None and latest_mermaid.status in {MermaidStatus.PAUSED, MermaidStatus.DRAFT}
        structural_terms = ("step", "loop", "gate", "branch", "path", "node", "box", "label", "exit condition", "planning elements")
        if in_mermaid_review and any(term in lowered for term in update_terms) and (
            proposal_pending or any(term in lowered for term in structural_terms)
        ):
            return True
        if lowered in {"do it", "do it.", "do that", "apply it", "make the changes", "make your changes", "go ahead", "use that"}:
            recent_turns = self._recent_conversation_turns(project_id=project_id, objective_id=objective_id)
            recent_text = "\n".join(turn.text.lower() for turn in recent_turns[-6:])
            return (
                "update the mermaid" in recent_text
                or "proposed mermaid update" in recent_text
                or "diagram should be revised" in recent_text
                or "revise that diagram" in recent_text
                or "make your changes to the diagram" in recent_text
            )
        return False

    def _run_output_sections(self, run_id: str) -> list[RunOutputSection]:
        run_dir = self.workspace_root / "runs" / run_id
        candidates: list[tuple[str, Path]] = []
        for artifact in self.store.list_artifacts(run_id):
            candidates.append((artifact.kind, Path(artifact.path)))
        for label, filename in [
            ("plan", "plan.txt"),
            ("report", "report.json"),
            ("compile_output", "compile_output.txt"),
            ("test_output", "test_output.txt"),
            ("worker_stdout", "worker.stdout.txt"),
            ("worker_stderr", "worker.stderr.txt"),
            ("llm_stdout", "llm.stdout.txt"),
            ("llm_stderr", "llm.stderr.txt"),
            ("codex_worker_stdout", "codex_worker.stdout.txt"),
            ("codex_worker_stderr", "codex_worker.stderr.txt"),
            ("atomicity_telemetry", "atomicity_telemetry.json"),
        ]:
            path = run_dir / filename
            if path.exists():
                candidates.append((label, path))
        seen: set[str] = set()
        sections: list[RunOutputSection] = []
        for label, path in candidates:
            resolved = str(path.resolve())
            if resolved in seen or not path.exists() or not path.is_file():
                continue
            seen.add(resolved)
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if not content:
                continue
            sections.append(
                RunOutputSection(
                    label=label.replace("_", " "),
                    path=resolved,
                    content=content,
                )
            )
        return sections

    def _summarize_run_output(self, run: Run, sections: list[RunOutputSection]) -> dict[str, object]:
        headline = f"Attempt {run.attempt} is {run.status.value}."
        highlights: list[str] = []
        section_map = {section.label: section.content for section in sections}

        if run.summary.strip():
            highlights.append(run.summary.strip())

        report_content = section_map.get("report")
        if report_content:
            try:
                report_payload = json.loads(report_content)
                worker_outcome = str(report_payload.get("worker_outcome") or "").strip()
                failure_category = str(report_payload.get("failure_category") or "").strip()
                if worker_outcome:
                    highlights.append(f"Worker outcome: {worker_outcome}.")
                if failure_category:
                    highlights.append(f"Failure category: {failure_category}.")
            except json.JSONDecodeError:
                highlights.append("A structured report exists, but it could not be parsed cleanly.")

        for label in ("test output", "compile output", "worker stderr", "codex worker stderr", "llm stderr"):
            content = section_map.get(label)
            if content:
                highlights.append(f"{label.title()}: {self._truncate_text(content, 160)}")

        status_value = run.status.value
        if status_value in {"failed", "blocked"}:
            interpretation = "The latest implementation attempt did not complete cleanly. Review the evidence before deciding whether to retry or investigate."
            recommended_next = "Ask the harness to summarize the failure or open investigation mode if the process feels wrong."
        elif status_value in {"analyzing", "working"}:
            interpretation = "The latest attempt is still in progress or has not reached a final decision yet."
            recommended_next = "Review the current evidence and decide whether to wait, redirect the harness, or investigate."
        elif status_value in {"completed"}:
            interpretation = "The latest implementation step completed. Review the result to decide whether to continue to the next slice."
            recommended_next = "Ask the harness what changed or continue execution if the result matches your intent."
        else:
            interpretation = "The latest run produced evidence, but the state still needs human review."
            recommended_next = "Review the summary first, then inspect raw evidence only if something looks off."

        return {
            "headline": headline,
            "interpretation": interpretation,
            "recommended_next": recommended_next,
            "highlights": highlights[:4],
        }


class _EventBus:
    """Simple pub/sub for SSE.  Clients register a queue; writers broadcast."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Queue[str | None]] = []

    def subscribe(self) -> Queue[str | None]:
        q: Queue[str | None] = Queue(maxsize=32)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: Queue[str | None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event: str) -> None:
        with self._lock:
            dead: list[Queue[str | None]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            for q in dead:
                try:
                    self._subscribers.remove(q)
                except ValueError:
                    pass


def _build_fastapi_app(data_service: HarnessUIDataService, event_bus: _EventBus):
    """Build a FastAPI application wired to the given data service and event bus."""
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse

    class _JSONResponse(JSONResponse):
        def render(self, content) -> bytes:
            return json.dumps(content, indent=2, sort_keys=True).encode("utf-8")

    app = FastAPI(title="Accruvia Harness", default_response_class=_JSONResponse)
    _NOCACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    cors_origins = tuple(
        origin.strip()
        for origin in os.environ.get(
            "ACCRUVIA_UI_CORS_ORIGINS",
            "http://127.0.0.1:3000,http://localhost:3000,http://127.0.0.1:4173,http://localhost:4173",
        ).split(",")
        if origin.strip()
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _dispatch(fn, *, status_code: int = 200, notify: bool = False):
        try:
            payload = fn()
        except ValueError as exc:
            return _JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return _JSONResponse({"error": str(exc)}, status_code=500)
        if notify:
            data_service.invalidate_harness_overview_cache()
            event_bus.publish("workspace-changed")
        return _JSONResponse(payload, status_code=status_code)

    @app.middleware("http")
    async def nocache_middleware(request, call_next):
        response = await call_next(request)
        for k, v in _NOCACHE.items():
            response.headers[k] = v
        return response

    # --- API root ---
    @app.get("/")
    def index():
        return {
            "service": "accruvia-harness-api",
            "commit": _GIT_COMMIT,
            "started_at": _SERVER_STARTED_AT,
            "docs_url": "/docs",
        }

    # --- API GET routes ---
    @app.get("/api/projects")
    def list_projects():
        return data_service.list_projects()

    @app.get("/api/projects/{project_ref}/workspace")
    def project_workspace(project_ref: str):
        return _dispatch(lambda: data_service.project_workspace(project_ref))

    @app.get("/api/projects/{project_ref}/summary")
    def project_summary(project_ref: str):
        return _dispatch(lambda: data_service.project_summary_fast(project_ref))

    @app.get("/api/projects/{project_ref}/objectives")
    def project_objectives(project_ref: str):
        return _dispatch(lambda: data_service.project_objectives_detail(project_ref))

    @app.get("/api/projects/{project_ref}/objectives/{objective_id}")
    def project_objective_detail(project_ref: str, objective_id: str):
        return _dispatch(lambda: data_service.project_objective_detail(project_ref, objective_id))

    @app.get("/api/projects/{project_ref}/token-performance")
    def project_token_performance(project_ref: str):
        return _dispatch(lambda: data_service.project_token_performance(project_ref))

    @app.get("/api/version")
    def version():
        return {"commit": _GIT_COMMIT, "started_at": _SERVER_STARTED_AT}

    @app.get("/api/harness")
    def harness_overview():
        return data_service.harness_overview()

    @app.get("/api/atomicity")
    def harness_atomicity():
        return _dispatch(lambda: data_service.harness_atomicity_overview())

    @app.get("/api/promotion")
    def harness_promotion():
        return _dispatch(lambda: data_service.harness_promotion_overview())

    @app.get("/api/runs/{run_id}/cli-output")
    def run_cli_output(run_id: str):
        return _dispatch(lambda: data_service.run_cli_output(run_id))

    # /conversation endpoint removed — mediation replaced by MCP server.

    @app.get("/api/tasks/{task_id}/insight")
    def task_insight(task_id: str):
        return _dispatch(lambda: data_service.task_failure_insight(task_id))

    @app.get("/api/projects/{project_id}/supervisor")
    def supervisor_status(project_id: str):
        return data_service.supervisor_status(project_id)

    @app.get("/api/events")
    async def sse_events():
        async def event_stream():
            q = event_bus.subscribe()
            try:
                while True:
                    try:
                        event = await asyncio.to_thread(q.get, timeout=15)
                    except Empty:
                        yield ":\n\n"
                        continue
                    if event is None:
                        break
                    yield f"data: {event}\n\n"
            except (asyncio.CancelledError, GeneratorExit):
                pass
            finally:
                event_bus.unsubscribe(q)
        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    # --- API POST routes ---
    @app.post("/api/projects/{project_id}/repo-settings")
    async def update_repo_settings(project_id: str, request):
        payload = await request.json()
        return _dispatch(lambda: data_service.update_project_repo_settings(
            project_id, promotion_mode=str(payload.get("promotion_mode") or ""),
            repo_provider=str(payload.get("repo_provider") or ""), repo_name=str(payload.get("repo_name") or ""),
            base_branch=str(payload.get("base_branch") or ""),
        ), notify=True)

    @app.post("/api/projects/{project_ref}/objectives", status_code=201)
    async def create_objective(project_ref: str, request: Request):
        payload = await request.json()
        return _dispatch(lambda: data_service.create_objective(
            project_ref, str(payload.get("title") or ""), str(payload.get("summary") or ""),
        ), status_code=201, notify=True)

    # /comments and /frustrations endpoints removed — mediation replaced by
    # MCP server. Users talk to Claude directly; Claude calls harness tools.

    @app.post("/api/objectives/{objective_id}/tasks", status_code=201)
    def create_linked_task(objective_id: str):
        return _dispatch(lambda: data_service.create_linked_task(objective_id), status_code=201, notify=True)

    @app.post("/api/objectives/{objective_id}/interrogation", status_code=201)
    def complete_interrogation(objective_id: str):
        return _dispatch(lambda: data_service.complete_interrogation_review(objective_id), status_code=201, notify=True)

    @app.post("/api/objectives/{objective_id}/promotion/force", status_code=201)
    async def force_promote(objective_id: str, request: Request):
        payload = await request.json()
        return _dispatch(lambda: data_service.force_promote_objective_review(
            objective_id, rationale=str(payload.get("rationale") or ""), author=str(payload.get("author") or "operator"),
        ), status_code=201, notify=True)

    @app.post("/api/objectives/{objective_id}/promote")
    def promote_objective(objective_id: str):
        return _dispatch(lambda: data_service.promote_objective_to_repo(objective_id), notify=True)

    @app.post("/api/tasks/{task_id}/promote")
    def promote_task(task_id: str):
        return _dispatch(lambda: data_service.promote_atomic_unit_to_repo(task_id), notify=True)

    @app.post("/api/objectives/{objective_id}/mermaid/proposal/accept", status_code=201)
    async def accept_mermaid(objective_id: str, request):
        payload = await request.json()
        return _dispatch(lambda: data_service.accept_mermaid_proposal(
            objective_id, str(payload.get("proposal_id") or ""),
        ), status_code=201, notify=True)

    @app.post("/api/objectives/{objective_id}/mermaid/proposal/reject", status_code=201)
    async def reject_mermaid(objective_id: str, request):
        payload = await request.json()
        return _dispatch(lambda: data_service.reject_mermaid_proposal(
            objective_id, str(payload.get("proposal_id") or ""),
            resolution=str(payload.get("resolution") or "refine"),
        ), status_code=201, notify=True)

    @app.post("/api/tasks/{task_id}/run", status_code=201)
    def run_task(task_id: str):
        return _dispatch(lambda: data_service.run_task(task_id), status_code=201, notify=True)

    @app.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        return _dispatch(lambda: data_service.retry_task(task_id), notify=True)

    @app.post("/api/tasks/{task_id}/failed-disposition")
    async def failed_task_disposition(task_id: str, request: Request):
        payload = await request.json()
        return _dispatch(
            lambda: data_service.apply_failed_task_disposition(
                task_id,
                disposition=str(payload.get("disposition") or ""),
                rationale=str(payload.get("rationale") or ""),
            ),
            notify=True,
        )

    @app.post("/api/projects/{project_id}/supervise", status_code=201)
    def start_supervisor(project_id: str):
        return _dispatch(lambda: data_service.start_supervisor(project_id), status_code=201, notify=True)

    @app.post("/api/projects/{project_id}/supervise/stop")
    def stop_supervisor(project_id: str):
        return _dispatch(lambda: data_service.stop_supervisor(project_id), notify=True)

    @app.post("/api/cli/command", status_code=201)
    async def cli_command(request):
        payload = await request.json()
        return _dispatch(lambda: data_service.run_cli_command(str(payload.get("command") or "")), status_code=201, notify=True)

    @app.post("/api/projects/{project_id}/retry-failed")
    def retry_all_failed(project_id: str):
        return _dispatch(lambda: data_service.retry_all_failed(project_id), notify=True)

    # --- API PUT routes ---
    @app.put("/api/objectives/{objective_id}/mermaid")
    async def update_mermaid(objective_id: str, request):
        payload = await request.json()
        return _dispatch(lambda: data_service.update_mermaid_artifact(
            objective_id, status=str(payload.get("status") or ""),
            summary=str(payload.get("summary") or ""), blocking_reason=str(payload.get("blocking_reason") or ""),
        ), notify=True)

    @app.put("/api/objectives/{objective_id}/intent")
    async def update_intent(objective_id: str, request: Request):
        payload = await request.json()
        return _dispatch(lambda: data_service.update_intent_model(
            objective_id, intent_summary=str(payload.get("intent_summary") or ""),
            success_definition=str(payload.get("success_definition") or ""),
            non_negotiables=list(payload.get("non_negotiables") or []),
            frustration_signals=list(payload.get("frustration_signals") or []),
        ), notify=True)

    return app


def _verify_install_path() -> None:
    """Refuse to start if the installed package points outside the source tree."""
    import accruvia_harness
    installed = Path(accruvia_harness.__file__).resolve().parent
    expected = Path(__file__).resolve().parent
    if installed != expected:
        raise RuntimeError(
            f"Installed package points to {installed}, expected {expected}. "
            f"Run: pip install -e . from the project root."
        )


def start_ui_server(ctx, *, host: str, port: int, open_browser: bool, project_ref: str | None = None) -> None:
    _verify_install_path()
    # Wire the LLM availability gate into the engine if config is available.
    if hasattr(ctx, "config") and ctx.config is not None:
        from .llm_availability import LLMAvailabilityGate
        from .onboarding import probe_llm_command
        gate = LLMAvailabilityGate(
            probe_fn=probe_llm_command,
            commands=[
                ("codex", ctx.config.llm_codex_command or ""),
                ("claude", ctx.config.llm_claude_command or ""),
                ("command", ctx.config.llm_command or ""),
            ],
        )
        ctx.engine.set_llm_gate(gate)
    data_service = HarnessUIDataService(ctx)
    if hasattr(ctx, "engine") and hasattr(ctx.engine, "queue"):
        ctx.engine.queue.post_task_callback = data_service.reconcile_task_workflow
    # The control plane must own a single canonical UI port. Silently hopping
    # to 9101/9102 creates split-brain status where runtime state and the real
    # serving process disagree about which API endpoint is authoritative.
    resolved_port = _resolve_ui_port(host, port)
    event_bus = _EventBus()
    app = _build_fastapi_app(data_service, event_bus)
    url = f"http://{host}:{resolved_port}/"
    if project_ref:
        project_id = resolve_project_ref(ctx, project_ref)
        url = f"{url}?project_id={project_id}"
    update_ui_runtime_state(
        ctx.config,
        host=host,
        preferred_port=port,
        resolved_port=resolved_port,
        project_ref=project_ref,
    )
    print(f"Harness API running at {url} (commit {_GIT_COMMIT})", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        print("Run the frontend separately with `npm --prefix frontend run dev`.", flush=True)
    # Background thread polls for database changes and pushes SSE events.
    _stop_change_detector = threading.Event()

    def _detect_changes() -> None:
        last_signature: str | None = None
        while not _stop_change_detector.wait(timeout=3):
            try:
                tasks = data_service.store.list_tasks()
                records = data_service.store.list_context_records()
                recent_records = records[-20:]
                sig = ";".join(
                    f"{t.id}:{t.status.value}:{t.updated_at.isoformat()}" for t in tasks
                )
                sig += "|ctx:" + ";".join(
                    f"{r.id}:{r.record_type}:{r.created_at.isoformat()}" for r in recent_records
                )
                if last_signature is not None and sig != last_signature:
                    data_service.invalidate_harness_overview_cache()
                    event_bus.publish("workspace-changed")
                last_signature = sig
            except Exception:
                pass

    change_thread = threading.Thread(target=_detect_changes, daemon=True)
    change_thread.start()

    _auto_start_supervisors(data_service, ctx)
    import uvicorn
    try:
        uvicorn.run(app, host=host, port=resolved_port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        clear_ui_runtime_state(ctx.config)
        _stop_change_detector.set()
        for project in data_service.store.list_projects():
            _BACKGROUND_SUPERVISOR.stop(project.id)


def _auto_start_supervisors(data_service: HarnessUIDataService, ctx) -> None:
    """Start background supervisors for projects with pending tasks, and resume stalled atomic generation."""
    external_supervisors_present = any(data_service._live_supervisor_records(project.id) for project in data_service.store.list_projects())
    cleared = 0
    recovered = {"runs": 0, "tasks": 0, "leases": 0}
    if not external_supervisors_present:
        # Only perform aggressive lease cleanup when the UI owns supervision.
        # If an external supervisor already exists, clearing leases here creates
        # a second scheduler and breaks single-owner control-plane semantics.
        with data_service.store.connect() as connection:
            cleared = connection.execute("DELETE FROM task_leases").rowcount
        recovered = data_service.store.recover_stale_state()
        if cleared or any(int(count or 0) > 0 for count in recovered.values()):
            print(f"  Startup recovery: cleared {cleared} leases, recovered {recovered}", flush=True)
    for project in data_service.store.list_projects():
        # Resume any stalled atomic generation
        for objective in data_service.store.list_objectives(project.id):
            try:
                data_service.reconcile_objective_workflow(objective.id)
                data_service._maybe_resume_atomic_generation(objective.id)
                data_service._maybe_resume_objective_review(objective.id)
            except Exception:
                pass
        # Start supervisor if there's work to do
        metrics = data_service.store.metrics_snapshot(project.id)
        pending = int(metrics.get("tasks_by_status", {}).get("pending", 0))
        active = int(metrics.get("tasks_by_status", {}).get("active", 0))
        if data_service._live_supervisor_records(project.id):
            continue
        if pending + active > 0:
            started = _BACKGROUND_SUPERVISOR.start(project.id, ctx.engine, watch=True)
            if started:
                print(f"  Auto-started harness for {project.name} ({pending} pending, {active} active)", flush=True)


def _resolve_ui_port(host: str, preferred_port: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, preferred_port))
        except OSError as exc:
            if exc.errno in {errno.EADDRINUSE, 48, 98}:
                raise OSError(
                    f"UI port {preferred_port} is already in use on {host}. "
                    "Refusing to fall back to another port because the control plane requires a single canonical API endpoint."
                ) from exc
            raise
    return preferred_port
