from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .agent_worker import run_agent_worker, run_validation
from .control_plane import ControlPlane
from .domain import (
    ContextRecord,
    ControlEvent,
    ControlLaneStateValue,
    ControlRecoveryAction,
    Event,
    ObjectiveStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    new_id,
)
from .llm import LLMExecutionError, LLMInvocation, LLMRouter
from .services.task_service import TaskService
from .store import SQLiteHarnessStore

if TYPE_CHECKING:
    from .engine import HarnessEngine


SA_WATCH_INTERVAL_SECONDS = 1200
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_HARNESS_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class SAWatchDecision:
    action: str
    reason: str
    confidence: float = 0.0
    target_lane: str | None = None
    escalate: bool = False
    task_title: str | None = None
    task_objective: str | None = None
    target_task_id: str | None = None


@dataclass(slots=True)
class SAWatchRepairResult:
    status: str
    run_id: str
    run_dir: Path
    summary: str
    changed_files: list[str]
    validation: dict[str, object]
    diagnostics: dict[str, object]
    stdout_summary: str | None = None


class SAWatchService:
    """Runs a periodic continuity review to keep work advancing."""

    def __init__(
        self,
        store: SQLiteHarnessStore,
        control_plane: ControlPlane,
        llm_router: LLMRouter | None,
        workspace_root: Path,
        *,
        interval_seconds: int = SA_WATCH_INTERVAL_SECONDS,
        engine: HarnessEngine | None = None,
        structural_progress_callback: Callable[[dict[str, object]], None] | None = None,
        post_repair_callback: Callable[[Task], None] | None = None,
        restart_stack: Callable[[dict[str, object]], dict[str, object] | None] | None = None,
        repair_runner: Callable[[Task, Run, Path], SAWatchRepairResult] | None = None,
    ) -> None:
        self.store = store
        self.control_plane = control_plane
        self.llm_router = llm_router
        self.workspace_root = workspace_root
        self.interval_seconds = interval_seconds
        self._last_invoked_at = 0.0
        self.engine = engine
        self.tasks = TaskService(store)
        self.structural_progress_callback = structural_progress_callback
        self.post_repair_callback = post_repair_callback
        self.restart_stack = restart_stack
        self.repair_runner = repair_runner or self._run_direct_repair

    @staticmethod
    def _local_time(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    def observe(self, event: dict[str, object]) -> dict[str, object] | None:
        if str(event.get("type") or "") != "sleeping":
            return None
        if time.monotonic() - self._last_invoked_at < self.interval_seconds:
            return None
        self._last_invoked_at = time.monotonic()
        return self.run_once()

    def run_once(self) -> dict[str, object]:
        packet = self._build_packet()
        if self.llm_router is None or not getattr(self.llm_router, "executors", {}):
            return self._record_skip("llm_router_unavailable", packet)
        try:
            report = self._invoke(packet)
        except (LLMExecutionError, ValueError, json.JSONDecodeError) as exc:
            return self._record_skip(f"llm_execution_failed:{exc}", packet)
        return self._apply(report, packet)

    def _build_packet(self) -> dict[str, object]:
        status = self.control_plane.status()
        continuity_signals = self._continuity_signals()
        structural_signal = continuity_signals[0] if continuity_signals else None
        now_local = datetime.now().astimezone()
        recent_events = [
            {
                "event_type": event.event_type,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "payload": event.payload,
                "created_at": self._local_time(event.created_at),
            }
            for event in self.store.list_control_events(limit=25)
        ]
        recent_runs = [
            {
                "run_id": item.id,
                "task_id": item.task_id,
                "status": item.status,
                "classification": item.classification,
                "started_at": self._local_time(item.started_at),
                "ended_at": self._local_time(item.ended_at),
            }
            for item in self.store.list_control_worker_runs()[:25]
        ]
        recent_actions = [
            {
                "action_type": item.action_type,
                "target_type": item.target_type,
                "target_id": item.target_id,
                "reason": item.reason,
                "result": item.result,
                "created_at": self._local_time(item.created_at),
            }
            for item in self.store.list_control_recovery_actions()[:25]
        ]
        evidence_manifest = self._evidence_manifest(structural_signal)
        return {
            "time_context": {
                "now_local": now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "timezone": now_local.tzname() or "local",
                "note": "All timestamps in this packet are local time.",
            },
            "status": status,
            "continuity_goal": "Work should keep moving. Detect loops, stalls, and dead workflow states, then restore forward progress safely.",
            "continuity_signals": continuity_signals,
            "structural_signal": structural_signal,
            "target_task": self._target_task_packet(structural_signal),
            "target_objective": self._target_objective_packet(structural_signal),
            "target_task_evidence": self._target_task_evidence(structural_signal),
            "target_objective_evidence": self._target_objective_evidence(structural_signal),
            "objective_summaries": self._objective_summaries(),
            "task_summary": self._task_summary(),
            "recent_events": recent_events,
            "recent_worker_runs": recent_runs,
            "recent_recovery_actions": recent_actions,
            "evidence_manifest_summary": evidence_manifest.get("summary"),
            "evidence_manifest": evidence_manifest,
            "allowed_actions": [
                "resume_worker",
                "restart_stack",
                "freeze_system",
                "repair_workflow_state",
                "repair_harness",
            ],
        }

    def _source_repo_root(self) -> str:
        import os
        import subprocess as _sp
        configured = os.environ.get("ACCRUVIA_SOURCE_REPO_ROOT")
        if configured:
            return configured
        try:
            return _sp.run(
                ["git", "rev-parse", "--show-toplevel"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        except Exception:
            return str(self.workspace_root)

    def _build_prompt(self, packet: dict[str, object], *, evidence_manifest_path: Path) -> str:
        repo_root = self._source_repo_root()
        db_path = str(self.store.db_path)
        return (
            "You are sa-watch, the recovery authority for the Accruvia harness.\n"
            "You are only invoked when the system is stuck.\n\n"
            "The control-loop has determined that forward progress has stopped. "
            "Your job is to diagnose WHY from the evidence below, then fix the root cause. "
            "You have full access to the harness codebase, database, logs, and runtime artifacts. "
            "Read whatever you need. Change whatever you need. There are no scope limits.\n\n"
            "Do not stop at observation. Inspect the full evidence manifest and prior related artifacts first, "
            "then make the smallest durable change that addresses the root cause.\n\n"
            "CRITICAL REQUIREMENTS:\n"
            f"- The harness source code is at: {repo_root}\n"
            f"- The harness database is at: {db_path}\n"
            f"- The full evidence manifest for this invocation is at: {evidence_manifest_path}\n"
            "- You are running in a temporary directory. Any code changes you make MUST be\n"
            f"  made in the source repo at {repo_root}, not in your current working directory.\n"
            "- Start by reading the evidence manifest, prior repair evidence, and repeated failure artifacts.\n"
            "- Do not claim write-permission or sandbox blockage unless the recorded workspace write probe failed.\n"
            "- If the same repair class already produced zero durable changes, do not repeat it.\n"
            "- If you cannot make a durable repair, emit a structured incident explanation in the report instead of prose-only recovery.\n"
            "- After fixing code: run the tests, then commit to the main branch and push.\n"
            "  Do NOT leave changes uncommitted — they will be lost.\n"
            "- After fixing database state: verify by querying the database that the stuck\n"
            "  condition is cleared. The control-loop checks for objective_stalled events\n"
            "  and paused objectives. If you don't clear the root cause, the control-loop\n"
            "  will detect stuck again and invoke you in an infinite loop.\n"
            "- Common database fixes: resolve or delete stale objective_stalled control events,\n"
            "  un-pause objectives that should be active, reset budget counters.\n\n"
            "Write a report as durable artifacts:\n"
            "1. Diagnosis: what broke and why, tied to specific evidence\n"
            "2. Actions taken: every change you made (code edits, database fixes, state resets, config changes)\n"
            "3. Result: proof that forward progress resumed, or what remains blocked and your next step\n\n"
            "Current system state:\n"
            f"{json.dumps(packet, indent=2, sort_keys=True)}\n"
        )

    def _invoke(self, packet: dict[str, object]) -> str:
        task = Task(
            id=new_id("task"),
            project_id="system",
            title="sa-watch recovery",
            objective="Diagnose stuck system and fix root cause.",
            status=TaskStatus.ACTIVE,
            strategy="sa_watch",
        )
        run = Run(
            id=new_id("run"),
            task_id=task.id,
            status=RunStatus.PLANNING,
            attempt=1,
            summary="sa-watch recovery",
        )
        run_dir = self.workspace_root / "control" / "sa_watch" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        evidence_manifest_path = run_dir / "evidence_manifest.json"
        evidence_manifest_path.write_text(
            json.dumps(packet.get("evidence_manifest", {}), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        result, _backend = self.llm_router.execute(
            LLMInvocation(task=task, run=run, prompt=self._build_prompt(packet, evidence_manifest_path=evidence_manifest_path), run_dir=run_dir)
        )
        report = result.response_text.strip()
        report_path = run_dir / "sa_watch_report.txt"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        return report

    def _apply(self, report: str, packet: dict[str, object]) -> dict[str, object]:
        self._record_action("recover", "system", "system", report[:500], "applied")
        return {
            "report": report,
            "status": self.control_plane.status(),
            "packet": packet,
        }

    def _record_skip(self, reason: str, packet: dict[str, object]) -> dict[str, object]:
        self._record_action("skip", "system", "system", reason, "recorded")
        return {"decision": {"action": "skip", "reason": reason}, "status": self.control_plane.status(), "packet": packet}

    def _usable_reason(self, reason: str) -> bool:
        return reason.strip().lower() not in {"", "sa-watch returned no reason"}

    def _record_action(self, action_type: str, target_type: str, target_id: str, reason: str, result: str) -> None:
        self.store.create_control_recovery_action(
            ControlRecoveryAction(
                id=new_id("recovery"),
                action_type=action_type,
                target_type=target_type,
                target_id=target_id,
                reason=reason,
                result=result,
            )
        )

    def _restart_stack(self, decision: SAWatchDecision) -> dict[str, object]:
        self._record_action("restart", "system", "system", decision.reason, "applied")
        if self.restart_stack is None:
            self.control_plane.mark_degraded("sa_watch_restart_unavailable")
            return self.control_plane.status()
        restart_status = self.restart_stack(
            {
                "reason": "sa_watch_requested_restart",
                "decision_reason": decision.reason,
                "target_lane": decision.target_lane,
                "target_task_id": decision.target_task_id,
            }
        )
        return restart_status or self.control_plane.status()

    def _repair_workflow_state(
        self,
        decision: SAWatchDecision,
        packet: dict[str, object],
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        signal = packet.get("structural_signal") or {}
        target_objective = self._target_objective_for_signal(signal)
        if target_objective is None and decision.target_task_id:
            task = self.store.get_task(decision.target_task_id)
            if task is not None and task.objective_id:
                target_objective = self.store.get_objective(task.objective_id)
        if target_objective is None:
            self._record_action("workflow_state_repair", "system", "system", f"missing_target:{decision.reason}", "ignored")
            return self.control_plane.status(), [{"kind": "noted_concern", "reason": f"missing_target:{decision.reason}"}]

        linked_tasks = [
            task
            for task in self.store.list_tasks(target_objective.project_id)
            if task.objective_id == target_objective.id
        ]
        legacy_tasks = [
            task
            for task in linked_tasks
            if task.strategy == "sa_structural_fix" and str(task.external_ref_type or "") == "sa_watch"
        ]
        if not legacy_tasks:
            self._record_action(
                "workflow_state_repair",
                "objective",
                target_objective.id,
                decision.reason,
                "noop",
            )
            return self.control_plane.status(), [{"kind": "observed", "reason": "workflow_state_already_clean"}]

        ignored_task_ids: list[str] = []
        waived_task_ids: list[str] = []
        rationale = (
            "Obsolete legacy sa-watch recovery task from the superseded structural-fix flow. "
            f"Reconciled by sa-watch workflow-state repair: {decision.reason}"
        )
        for task in legacy_tasks:
            metadata = dict(task.external_ref_metadata)
            workflow_disposition = (
                metadata.get("workflow_state_disposition")
                if isinstance(metadata.get("workflow_state_disposition"), dict)
                else None
            )
            if not workflow_disposition or str(workflow_disposition.get("kind") or "").strip() != "ignore_obsolete":
                metadata["workflow_state_disposition"] = {
                    "kind": "ignore_obsolete",
                    "rationale": rationale,
                    "source": "sa_watch",
                }
                self.store.update_task_external_metadata(task.id, metadata)
                ignored_task_ids.append(task.id)
            failed_disposition = (
                metadata.get("failed_task_disposition")
                if isinstance(metadata.get("failed_task_disposition"), dict)
                else None
            )
            if task.status == TaskStatus.FAILED and (
                not failed_disposition or str(failed_disposition.get("kind") or "").strip() != "waive_obsolete"
            ):
                self.tasks.apply_failed_task_disposition(
                    task_id=task.id,
                    disposition="waive_obsolete",
                    rationale=rationale,
                )
                waived_task_ids.append(task.id)

        phase = self.store.update_objective_phase(target_objective.id)
        objective_after = self.store.get_objective(target_objective.id)
        if objective_after is not None and objective_after.status == ObjectiveStatus.RESOLVED:
            self.store.update_objective_status(target_objective.id, ObjectiveStatus.PLANNING)
            objective_after = self.store.get_objective(target_objective.id)

        payload = {
            "objective_id": target_objective.id,
            "ignored_task_ids": ignored_task_ids,
            "waived_task_ids": waived_task_ids,
            "reason": decision.reason,
            "objective_status": objective_after.status.value if objective_after is not None else None,
        }
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="sa_watch_workflow_state_repair",
                project_id=target_objective.project_id,
                objective_id=target_objective.id,
                visibility="operator_visible",
                author_type="system",
                author_id="sa-watch",
                content=f"sa-watch reconciled obsolete workflow state for objective {target_objective.title}",
                metadata=payload,
            )
        )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="objective",
                entity_id=target_objective.id,
                event_type="sa_watch_workflow_state_repaired",
                payload=payload,
            )
        )
        restart_status = None
        effects: list[dict[str, object]] = [
            {
                "kind": "workflow_state_repaired",
                "objective_id": target_objective.id,
                "ignored_task_ids": ignored_task_ids,
                "waived_task_ids": waived_task_ids,
            }
        ]
        if self.restart_stack is not None:
            restart_status = self.restart_stack(
                {
                    "reason": "sa_watch_workflow_state_repaired",
                    "objective_id": target_objective.id,
                    "ignored_task_ids": ignored_task_ids,
                    "waived_task_ids": waived_task_ids,
                }
            )
            effects.append({"kind": "stack_restart_requested", "reason": "sa_watch_workflow_state_repaired"})
        self._record_action("workflow_state_repair", "objective", target_objective.id, decision.reason, "verified")
        return restart_status or self.control_plane.status(), effects

    def _repair_harness(self, decision: SAWatchDecision, packet: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
        signal = packet.get("structural_signal") or {}
        before_progress = self._progress_snapshot(signal)
        target_task = self.store.get_task(str(decision.target_task_id or "")) if decision.target_task_id else None
        if target_task is None:
            target_task = self.store.get_task(str(signal.get("task_id") or ""))
        target_objective = self._target_objective_for_signal(signal)
        if target_task is None and target_objective is None:
            self._record_action("escalate", "system", "system", f"missing_target:{decision.reason}", "recorded")
            return self.control_plane.status(), [{"kind": "noted_concern", "reason": f"missing_target:{decision.reason}"}]
        objective_id = target_task.objective_id if target_task is not None else target_objective.id
        project_id = target_task.project_id if target_task is not None else target_objective.project_id
        classification = (
            self._latest_classification_for_task(target_task.id)
            if target_task is not None
            else str(signal.get("kind") or "structural_stall")
        )
        failure_family = self._failure_family(structural_signal=signal, classification=classification, task=target_task)
        repair_task_id = new_id("sa_watch_repair")
        repair_run = Run(
            id=new_id("run"),
            task_id=repair_task_id,
            status=RunStatus.WORKING,
            attempt=1,
            summary=f"sa-watch direct repair for {classification}",
        )
        repair_run_dir = self.workspace_root / "control" / "sa_watch_repairs" / repair_run.id
        repair_run_dir.mkdir(parents=True, exist_ok=True)
        write_probe = self._workspace_write_probe(repo_root=_HARNESS_REPO_ROOT, probe_key=repair_run.id)
        repair_memory = self._related_repair_memory(objective_id=objective_id, failure_family=failure_family)
        manifest_path = repair_run_dir / "evidence_manifest.json"
        manifest_path.write_text(
            json.dumps(
                self._repair_evidence_manifest(
                    signal=signal,
                    target_task=target_task,
                    target_objective=target_objective,
                    classification=classification,
                    failure_family=failure_family,
                    write_probe=write_probe,
                    repair_memory=repair_memory,
                ),
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        repair_task = Task(
            id=repair_task_id,
            project_id=project_id,
            objective_id=objective_id,
            title=decision.task_title or self._default_repair_title(target_task, target_objective, classification),
            objective=decision.task_objective or self._default_repair_objective(
                target_task,
                target_objective,
                classification,
                manifest_path=manifest_path,
            ),
            priority=max(150, int(target_task.priority if target_task is not None else target_objective.priority)),
            validation_profile=target_task.validation_profile if target_task is not None else "generic",
            validation_mode=target_task.validation_mode if target_task is not None else "default_focused",
            scope=dict(target_task.scope) if target_task is not None else {},
            external_ref_metadata={
                "sa_watch_context": {
                    "evidence_manifest_path": str(manifest_path),
                    "failure_family": failure_family,
                    "workspace_write_probe": write_probe,
                    "related_repair_memory": repair_memory,
                }
            },
            strategy="sa_watch_direct_repair",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        effects: list[dict[str, object]] = []
        self._persist_repair_start(repair_task, repair_run, decision=decision, signal=signal)
        self.control_plane.pause_lane("worker", reason=f"sa_watch_repair:{decision.reason}")
        try:
            repair_result = self.repair_runner(repair_task, repair_run, _HARNESS_REPO_ROOT)
        except Exception as exc:
            self._record_action("repair", "system", "system", f"{decision.reason}:execute_failed:{exc}", "failed")
            self.control_plane.mark_degraded("sa_watch_repair_failed")
            failed_result = SAWatchRepairResult(
                status="failed",
                run_id=repair_run.id,
                run_dir=self.workspace_root / "control" / "sa_watch_repairs" / repair_run.id,
                summary=str(exc),
                changed_files=[],
                validation={},
                diagnostics={"exception": str(exc)},
            )
            self._persist_repair_completion(repair_task, repair_run, failed_result, movement_restored=False)
            self._record_repair_evidence(
                repair_task=repair_task,
                repair_run=repair_run,
                decision=decision,
                signal=signal,
                repair_result=failed_result,
                before_progress=before_progress,
                after_progress=self._progress_snapshot(signal),
                movement_restored=False,
            )
            effects.append({"kind": "repair_failed", "reason": str(exc)})
            return self.control_plane.status(), effects
        if self.post_repair_callback is not None:
            self.post_repair_callback(repair_task)
        after_progress = self._progress_snapshot(signal)
        movement_restored = self._verified_recovery(
            repair_result,
            signal=signal,
            before=before_progress,
            after=after_progress,
        )
        self._persist_repair_completion(repair_task, repair_run, repair_result, movement_restored=movement_restored)
        self._record_repair_evidence(
            repair_task=repair_task,
            repair_run=repair_run,
            decision=decision,
            signal=signal,
            repair_result=repair_result,
            before_progress=before_progress,
            after_progress=after_progress,
            movement_restored=movement_restored,
        )
        if repair_result.status != "validated":
            self._record_action("repair", "system", "system", decision.reason, repair_result.status)
            self.control_plane.mark_degraded("sa_watch_repair_failed")
            self.store.create_control_event(
                ControlEvent(
                    id=new_id("control_event"),
                    event_type="human_escalation_required",
                    entity_type="system",
                    entity_id="system",
                    producer="sa-watch",
                    payload={
                        "reason": "sa-watch could not complete a validated architectural repair.",
                        "objective_id": objective_id,
                        "repair_run_id": repair_run.id,
                    },
                    idempotency_key=new_id("event_key"),
                )
            )
            effects.append({"kind": "repair_failed", "reason": repair_result.summary})
            return self.control_plane.status(), effects
        if not movement_restored:
            self._record_action("escalate", "system", "system", decision.reason, "verification_failed")
            self.control_plane.mark_degraded("sa_watch_no_forward_progress")
            self.store.create_control_event(
                ControlEvent(
                    id=new_id("control_event"),
                    event_type="human_escalation_required",
                    entity_type="system",
                    entity_id="system",
                    producer="sa-watch",
                    payload={
                        "reason": "sa-watch repaired and validated the harness locally but could not verify restored pipeline movement.",
                        "objective_id": objective_id,
                        "repair_run_id": repair_run.id,
                    },
                    idempotency_key=new_id("event_key"),
                )
            )
            effects.append({"kind": "noted_concern", "reason": "repair_validated_but_pipeline_still_stalled"})
            return self.control_plane.status(), effects
        restart_status = None
        if self.restart_stack is not None:
            restart_status = self.restart_stack(
                {
                    "reason": "sa_watch_repair_verified",
                    "objective_id": objective_id,
                    "target_task_id": target_task.id if target_task is not None else None,
                    "repair_run_id": repair_run.id,
                }
            )
            effects.append({"kind": "stack_restart_requested", "reason": "sa_watch_repair_verified"})
        self._record_action("repair", "system", "system", decision.reason, "verified")
        self.control_plane.resume_lane("worker", reason="sa_watch_repair_verified")
        self.control_plane.mark_healthy(reason="sa_watch_repair_verified")
        effects.append({"kind": "repair_validated", "run_id": repair_run.id})
        effects.append({"kind": "lane_resumed", "lane": "worker", "reason": "sa_watch_repair_verified"})
        return restart_status or self.control_plane.status(), effects

    def _persist_repair_start(
        self,
        repair_task: Task,
        repair_run: Run,
        *,
        decision: SAWatchDecision,
        signal: dict[str, object],
    ) -> None:
        self.store.create_task(repair_task)
        self.store.create_run(repair_run)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=repair_task.id,
                event_type="sa_watch_direct_repair_started",
                payload={
                    "run_id": repair_run.id,
                    "reason": decision.reason,
                    "signal": signal,
                },
            )
        )

    def _persist_repair_completion(
        self,
        repair_task: Task,
        repair_run: Run,
        repair_result: SAWatchRepairResult,
        *,
        movement_restored: bool,
    ) -> None:
        if repair_result.status == "validated" and movement_restored:
            final_run_status = RunStatus.COMPLETED
            final_task_status = TaskStatus.COMPLETED
        elif repair_result.status == "blocked":
            final_run_status = RunStatus.BLOCKED
            final_task_status = TaskStatus.FAILED
        else:
            final_run_status = RunStatus.FAILED
            final_task_status = TaskStatus.FAILED
        self.store.update_run(
            Run(
                id=repair_run.id,
                task_id=repair_run.task_id,
                status=final_run_status,
                attempt=repair_run.attempt,
                summary=repair_result.summary,
                branch_id=repair_run.branch_id,
                created_at=repair_run.created_at,
                updated_at=datetime.now(UTC),
            )
        )
        self.store.update_task_status(repair_task.id, final_task_status)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="run",
                entity_id=repair_run.id,
                event_type="sa_watch_direct_repair_finished",
                payload={
                    "task_id": repair_task.id,
                    "result": repair_result.status,
                    "movement_restored": movement_restored,
                    "summary": repair_result.summary,
                },
            )
        )

    def _target_task_packet(self, structural_signal: dict[str, object] | None) -> dict[str, object] | None:
        if not structural_signal:
            return None
        task_id = str(structural_signal.get("task_id") or "")
        if not task_id:
            return None
        task = self.store.get_task(task_id)
        if task is None:
            return None
        objective = self.store.get_objective(task.objective_id) if task.objective_id else None
        return {
            "task_id": task.id,
            "title": task.title,
            "objective": task.objective,
            "strategy": task.strategy,
            "project_id": task.project_id,
            "objective_id": task.objective_id,
            "objective_status": objective.status.value if objective is not None else None,
        }

    def _target_objective_packet(self, structural_signal: dict[str, object] | None) -> dict[str, object] | None:
        objective = self._target_objective_for_signal(structural_signal)
        if objective is None:
            return None
        project = self.store.get_project(objective.project_id)
        return {
            "objective_id": objective.id,
            "project_id": objective.project_id,
            "project_name": project.name if project is not None else None,
            "title": objective.title,
            "summary": objective.summary,
            "status": objective.status.value,
        }

    def _target_task_evidence(self, structural_signal: dict[str, object] | None) -> dict[str, object] | None:
        if not structural_signal:
            return None
        task_id = str(structural_signal.get("task_id") or "")
        if not task_id:
            return None
        latest_breadcrumb = next(iter(self.store.list_control_breadcrumbs(entity_type="task", entity_id=task_id)), None)
        latest_run = next(iter(self.store.list_control_worker_runs(task_id=task_id)), None)
        return {
            "latest_breadcrumb_path": latest_breadcrumb.path if latest_breadcrumb is not None else None,
            "latest_classification": latest_breadcrumb.classification if latest_breadcrumb is not None else None,
            "latest_run_id": latest_run.id if latest_run is not None else None,
            "latest_run_status": latest_run.status if latest_run is not None else None,
        }

    def _target_objective_evidence(self, structural_signal: dict[str, object] | None) -> dict[str, object] | None:
        objective = self._target_objective_for_signal(structural_signal)
        if objective is None:
            return None
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
        latest_atomic = self._latest_atomic_generation_state(objective.id)
        failed_counts = self._failed_task_counts(linked_tasks)
        return {
            "linked_task_counts": {
                "pending": sum(1 for task in linked_tasks if task.status == TaskStatus.PENDING),
                "active": sum(1 for task in linked_tasks if task.status == TaskStatus.ACTIVE),
                "completed": sum(1 for task in linked_tasks if task.status == TaskStatus.COMPLETED),
                "failed_unresolved": failed_counts["failed_unresolved"],
                "failed_historical": failed_counts["failed_waived_or_superseded"],
            },
            "latest_atomic_generation": {
                **latest_atomic,
                "last_activity_at": self._local_time(latest_atomic["last_activity_at"]) if latest_atomic is not None else None,
            }
            if latest_atomic is not None
            else None,
        }

    def _progress_snapshot(self, structural_signal: dict[str, object] | None) -> dict[str, object]:
        objective = self._target_objective_for_signal(structural_signal)
        if objective is None:
            return {"objective_id": None}
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
        failed_counts = self._failed_task_counts(linked_tasks)
        return {
            "objective_id": objective.id,
            "status": objective.status.value,
            "pending": sum(1 for task in linked_tasks if task.status == TaskStatus.PENDING),
            "active": sum(1 for task in linked_tasks if task.status == TaskStatus.ACTIVE),
            "completed": sum(1 for task in linked_tasks if task.status == TaskStatus.COMPLETED),
            "failed_unresolved": failed_counts["failed_unresolved"],
            "failed_historical": failed_counts["failed_waived_or_superseded"],
            "stale_atomic_generation": bool((self._latest_atomic_generation_state(objective.id) or {}).get("is_stale")),
        }

    def _forward_progress_resumed(self, structural_signal: dict[str, object] | None, before: dict[str, object]) -> bool:
        objective = self._target_objective_for_signal(structural_signal)
        if objective is None:
            return True
        after = self._progress_snapshot(structural_signal)
        if objective.status == ObjectiveStatus.RESOLVED:
            return True
        if after["pending"] > before.get("pending", 0) or after["active"] > before.get("active", 0):
            return True
        if before.get("stale_atomic_generation") and not after.get("stale_atomic_generation"):
            return True
        if after.get("failed_unresolved", 0) < before.get("failed_unresolved", 0):
            return True
        if before.get("status") != after.get("status") and after.get("status") in {
            ObjectiveStatus.PLANNING.value,
            ObjectiveStatus.EXECUTING.value,
        }:
            return True
        return False

    def _repair_is_durable(self, repair_result: SAWatchRepairResult) -> bool:
        if repair_result.changed_files:
            return True
        diagnostics = repair_result.diagnostics if isinstance(repair_result.diagnostics, dict) else {}
        return any(
            bool(diagnostics.get(key))
            for key in (
                "db_reconciliation_applied",
                "workflow_state_reconciled",
                "stack_restart_requested",
                "explicit_reconciliation_applied",
            )
        )

    def _verified_recovery(
        self,
        repair_result: SAWatchRepairResult,
        *,
        signal: dict[str, object] | None,
        before: dict[str, object],
        after: dict[str, object],
    ) -> bool:
        del after
        return (
            repair_result.status == "validated"
            and self._repair_is_durable(repair_result)
            and self._forward_progress_resumed(signal, before)
        )

    def _continuity_signals(self) -> list[dict[str, object]]:
        signals: list[dict[str, object]] = []
        for signal in (
            self._stale_atomic_generation_signal(),
            self._repeated_failure_signal(),
            self._low_value_churn_signal(),
            self._no_progress_signal(),
            self._objective_stalled_signal(),
            self._worker_paused_signal(),
            self._workflow_gap_signal(),
        ):
            if signal is not None:
                signals.append(signal)
        return signals

    def _repeated_failure_signal(self) -> dict[str, object] | None:
        recent_runs = self.store.list_control_worker_runs()
        grouped: dict[tuple[str, str, str], list[object]] = {}
        ignorable = {"artifact_contract_failure"}
        for run in recent_runs:
            if not run.task_id or not run.classification:
                continue
            if run.classification in ignorable:
                continue
            grouped.setdefault((str(run.objective_id or ""), run.task_id, run.classification), []).append(run)
        for (objective_id, task_id, classification), runs in grouped.items():
            if len(runs) >= 2:
                return {
                    "kind": "repeated_failure",
                    "task_id": task_id,
                    "objective_id": objective_id,
                    "classification": classification,
                    "count": len(runs),
                }
        return None

    def _no_progress_signal(self) -> dict[str, object] | None:
        escalations = self.store.list_control_recovery_actions(target_type="system", target_id="system")
        for action in escalations[:5]:
            if action.reason != "no_progress":
                continue
            events = self.store.list_control_events(event_type="human_escalation_required", limit=5)
            for event in events:
                if event.payload.get("reason") == "Three completed coding runs did not advance the objective to a mergeable state.":
                    objective_id = str(event.payload.get("objective_id") or "")
                    if not objective_id:
                        continue
                    recent_objective_runs = [item for item in self.store.list_control_worker_runs() if item.objective_id == objective_id]
                    if recent_objective_runs:
                        return {
                            "kind": "no_progress",
                            "objective_id": objective_id,
                            "task_id": recent_objective_runs[0].task_id,
                            "count": len(recent_objective_runs[:3]),
                        }
        return None

    def _low_value_churn_signal(self) -> dict[str, object] | None:
        summary_text = "Artifacts were insufficient; retry within bounded task budget."
        recent_terminal_runs: list[tuple[Run, Task]] = []
        for task in self.store.list_tasks():
            if not task.objective_id:
                continue
            for run in self.store.list_runs(task.id):
                if run.status not in {RunStatus.FAILED, RunStatus.BLOCKED}:
                    continue
                recent_terminal_runs.append((run, task))
        recent_terminal_runs.sort(key=lambda item: item[0].updated_at, reverse=True)
        by_objective: dict[str, list[tuple[Run, Task]]] = {}
        for run, task in recent_terminal_runs[:16]:
            if task.objective_id:
                by_objective.setdefault(task.objective_id, []).append((run, task))
        for objective_id, entries in by_objective.items():
            matching = [(run, task) for run, task in entries if summary_text in run.summary]
            if len(matching) < 3:
                continue
            objective = self.store.get_objective(objective_id)
            if objective is None or objective.status == ObjectiveStatus.RESOLVED:
                continue
            linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective_id]
            pending_count = sum(1 for task in linked_tasks if task.status == TaskStatus.PENDING)
            active_count = sum(1 for task in linked_tasks if task.status == TaskStatus.ACTIVE)
            if pending_count == 0:
                continue
            most_recent_run, most_recent_task = matching[0]
            oldest_considered_run = matching[min(2, len(matching) - 1)][0]
            completed_since_first_failure = any(
                task.status == TaskStatus.COMPLETED and task.updated_at >= oldest_considered_run.updated_at
                for task in linked_tasks
            )
            if completed_since_first_failure:
                continue
            return {
                "kind": "low_value_churn",
                "objective_id": objective_id,
                "task_id": most_recent_task.id,
                "count": len(matching[:5]),
                "run_ids": [run.id for run, _task in matching[:5]],
                "pending_tasks": pending_count,
                "active_tasks": active_count,
                "summary": summary_text,
            }
        return None

    def _objective_stalled_signal(self) -> dict[str, object] | None:
        stalled = self.store.list_control_events(event_type="objective_stalled")
        recent_cutoff = datetime.now(UTC) - timedelta(minutes=30)
        for event in stalled:
            if event.created_at < recent_cutoff:
                continue
            return {
                "kind": "objective_stalled",
                "objective_id": event.entity_id,
                "payload": event.payload,
            }
        return None

    def _worker_paused_signal(self) -> dict[str, object] | None:
        lane = self.store.get_control_lane_state("worker")
        if lane is None or lane.state != ControlLaneStateValue.PAUSED:
            return None
        return {
            "kind": "worker_paused",
            "lane_reason": lane.reason,
        }

    def _workflow_gap_signal(self) -> dict[str, object] | None:
        unresolved = [objective for objective in self.store.list_objectives() if objective.status != ObjectiveStatus.RESOLVED]
        if not unresolved:
            return None
        pending_or_active = [task for task in self.store.list_tasks() if task.status in {TaskStatus.PENDING, TaskStatus.ACTIVE}]
        if pending_or_active:
            return None
        oldest = sorted(unresolved, key=lambda item: item.updated_at)[0]
        latest_atomic = self._latest_atomic_generation_state(oldest.id)
        return {
            "kind": "workflow_gap",
            "objective_id": oldest.id,
            "objective_status": oldest.status.value,
            "last_activity_at": self._local_time(oldest.updated_at),
            "latest_atomic_generation": latest_atomic["status"] if latest_atomic is not None else None,
        }

    def _objective_summaries(self) -> list[dict[str, object]]:
        summaries: list[dict[str, object]] = []
        for objective in self.store.list_objectives():
            linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
            latest_atomic = self._latest_atomic_generation_state(objective.id)
            failed_counts = self._failed_task_counts(linked_tasks)
            summaries.append(
                {
                    "objective_id": objective.id,
                    "title": objective.title,
                    "status": objective.status.value,
                    "updated_at": self._local_time(objective.updated_at),
                    "pending_tasks": sum(1 for task in linked_tasks if task.status == TaskStatus.PENDING),
                    "active_tasks": sum(1 for task in linked_tasks if task.status == TaskStatus.ACTIVE),
                    "completed_tasks": sum(1 for task in linked_tasks if task.status == TaskStatus.COMPLETED),
                    "failed_unresolved_tasks": failed_counts["failed_unresolved"],
                    "failed_historical_tasks": failed_counts["failed_waived_or_superseded"],
                    "latest_atomic_generation": {
                        **latest_atomic,
                        "last_activity_at": self._local_time(latest_atomic["last_activity_at"]) if latest_atomic is not None else None,
                    }
                    if latest_atomic is not None
                    else None,
                }
            )
        return summaries

    def _task_summary(self) -> dict[str, int]:
        tasks = self.store.list_tasks()
        failed_counts = self._failed_task_counts(tasks)
        return {
            "pending": sum(1 for task in tasks if task.status == TaskStatus.PENDING),
            "active": sum(1 for task in tasks if task.status == TaskStatus.ACTIVE),
            "completed": sum(1 for task in tasks if task.status == TaskStatus.COMPLETED),
            "failed_unresolved": failed_counts["failed_unresolved"],
            "failed_historical": failed_counts["failed_waived_or_superseded"],
        }

    def _failed_task_counts(self, tasks: list[Task]) -> dict[str, int]:
        completed_external_refs = {
            (str(task.external_ref_type or "").strip(), str(task.external_ref_id or "").strip())
            for task in tasks
            if task.status == TaskStatus.COMPLETED
            and str(task.external_ref_type or "").strip()
            and str(task.external_ref_id or "").strip()
        }
        completed_review_dimensions = {
            str((task.external_ref_metadata or {}).get("objective_review_remediation", {}).get("dimension") or "").strip()
            for task in tasks
            if task.status == TaskStatus.COMPLETED
            and isinstance(task.external_ref_metadata, dict)
            and isinstance(task.external_ref_metadata.get("objective_review_remediation"), dict)
            and str((task.external_ref_metadata or {}).get("objective_review_remediation", {}).get("dimension") or "").strip()
        }
        unresolved = 0
        historical = 0
        total = 0
        for task in tasks:
            if task.status != TaskStatus.FAILED:
                continue
            total += 1
            if self._failed_task_is_historical(task, completed_external_refs, completed_review_dimensions):
                historical += 1
            else:
                unresolved += 1
        return {
            "failed_total": total,
            "failed_unresolved": unresolved,
            "failed_waived_or_superseded": historical,
        }

    def _failed_task_is_historical(
        self,
        task: Task,
        completed_external_refs: set[tuple[str, str]],
        completed_review_dimensions: set[str],
    ) -> bool:
        if task.status != TaskStatus.FAILED:
            return False
        metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
        disposition = metadata.get("failed_task_disposition") if isinstance(metadata.get("failed_task_disposition"), dict) else None
        if disposition and str(disposition.get("kind") or "").strip() == "waive_obsolete":
            return True
        remediation = metadata.get("objective_review_remediation") if isinstance(metadata.get("objective_review_remediation"), dict) else {}
        dimension = str(remediation.get("dimension") or "").strip()
        normalized_type = str(task.external_ref_type or "").strip()
        normalized_id = str(task.external_ref_id or "").strip()
        return bool(
            normalized_type == "objective_review"
            and (
                (normalized_id and (normalized_type, normalized_id) in completed_external_refs)
                or (dimension and dimension in completed_review_dimensions)
            )
        )

    def _failure_family(self, *, structural_signal: dict[str, object] | None, classification: str | None, task: Task | None) -> str:
        objective_id = str((structural_signal or {}).get("objective_id") or "")
        if not objective_id and task is not None:
            objective_id = str(task.objective_id or "")
        task_id = str(task.id) if task is not None else str((structural_signal or {}).get("task_id") or "")
        signal_kind = str((structural_signal or {}).get("kind") or "structural_stall")
        raw = "|".join([objective_id, task_id, str(classification or ""), signal_kind])
        return sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _related_repair_memory(self, *, objective_id: str | None, failure_family: str) -> list[dict[str, object]]:
        if not objective_id:
            return []
        memories: list[dict[str, object]] = []
        for record in reversed(self.store.list_context_records(objective_id=objective_id, record_type="sa_watch_repair")):
            metadata = record.metadata if isinstance(record.metadata, dict) else {}
            memory = metadata.get("repair_memory") if isinstance(metadata.get("repair_memory"), dict) else {}
            if not memory:
                continue
            if str(memory.get("failure_family") or "") != failure_family:
                continue
            memories.append(memory)
            if len(memories) >= 5:
                break
        return memories

    def _workspace_write_probe(self, *, repo_root: Path, probe_key: str) -> dict[str, object]:
        probe_dir = repo_root / ".accruvia-harness" / "control"
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe_path = probe_dir / f"sa_watch_probe_{probe_key}.tmp"
        payload = f"probe:{probe_key}\n"
        try:
            probe_path.write_text(payload, encoding="utf-8")
            observed = probe_path.read_text(encoding="utf-8")
            probe_path.unlink(missing_ok=True)
            return {
                "ok": observed == payload,
                "path": str(probe_path),
                "error": "" if observed == payload else "probe_read_mismatch",
            }
        except Exception as exc:
            try:
                probe_path.unlink(missing_ok=True)
            except Exception:
                pass
            return {"ok": False, "path": str(probe_path), "error": str(exc)}

    def _evidence_manifest(self, structural_signal: dict[str, object] | None) -> dict[str, object]:
        target_task = self.store.get_task(str((structural_signal or {}).get("task_id") or "")) if structural_signal else None
        target_objective = self._target_objective_for_signal(structural_signal)
        objective_id = str(target_objective.id) if target_objective is not None else str(target_task.objective_id or "") if target_task is not None else ""
        linked_tasks = (
            [task for task in self.store.list_tasks(target_objective.project_id) if task.objective_id == target_objective.id]
            if target_objective is not None
            else []
        )
        failed_counts = self._failed_task_counts(linked_tasks)
        classification = (
            self._latest_classification_for_task(target_task.id)
            if target_task is not None
            else str((structural_signal or {}).get("kind") or "")
        )
        failure_family = self._failure_family(structural_signal=structural_signal, classification=classification, task=target_task)
        completed_external_refs = {
            (str(task.external_ref_type or "").strip(), str(task.external_ref_id or "").strip())
            for task in linked_tasks
            if task.status == TaskStatus.COMPLETED
            and str(task.external_ref_type or "").strip()
            and str(task.external_ref_id or "").strip()
        }
        completed_review_dimensions = {
            str((task.external_ref_metadata or {}).get("objective_review_remediation", {}).get("dimension") or "").strip()
            for task in linked_tasks
            if task.status == TaskStatus.COMPLETED
            and isinstance(task.external_ref_metadata, dict)
            and isinstance(task.external_ref_metadata.get("objective_review_remediation"), dict)
            and str((task.external_ref_metadata or {}).get("objective_review_remediation", {}).get("dimension") or "").strip()
        }
        objective_runs = [
            run for run in self.store.list_control_worker_runs()
            if objective_id and str(run.objective_id or "") == objective_id
        ]
        return {
            "summary": {
                "signal_kind": str((structural_signal or {}).get("kind") or ""),
                "target_objective_id": objective_id or None,
                "target_task_id": target_task.id if target_task is not None else None,
                "failure_family": failure_family,
                "unresolved_failed_tasks": failed_counts["failed_unresolved"],
                "historical_failed_tasks": failed_counts["failed_waived_or_superseded"],
                "objective_run_count": len(objective_runs),
                "related_repair_memory_count": len(self._related_repair_memory(objective_id=objective_id, failure_family=failure_family)),
            },
            "paths": {
                "repo_root": self._source_repo_root(),
                "db_path": str(self.store.db_path),
                "runs_root": str(self.workspace_root / "runs"),
                "sa_watch_repairs_root": str(self.workspace_root / "control" / "sa_watch_repairs"),
            },
            "target_objective_runs": [
                {
                    "run_id": run.id,
                    "task_id": run.task_id,
                    "status": run.status,
                    "classification": run.classification,
                    "breadcrumb_path": run.breadcrumb_path,
                    "started_at": self._local_time(run.started_at),
                    "ended_at": self._local_time(run.ended_at),
                }
                for run in objective_runs
            ],
            "target_task_runs": [
                {
                    "run_id": run.id,
                    "status": run.status,
                    "classification": run.classification,
                    "breadcrumb_path": run.breadcrumb_path,
                    "started_at": self._local_time(run.started_at),
                    "ended_at": self._local_time(run.ended_at),
                }
                for run in (self.store.list_control_worker_runs(task_id=target_task.id) if target_task is not None else [])
            ],
            "related_repair_memory": self._related_repair_memory(objective_id=objective_id, failure_family=failure_family),
            "unresolved_failed_tasks": [
                {
                    "task_id": task.id,
                    "title": task.title,
                    "strategy": task.strategy,
                    "updated_at": self._local_time(task.updated_at),
                    "external_ref_type": task.external_ref_type,
                    "external_ref_id": task.external_ref_id,
                }
                for task in linked_tasks
                if task.status == TaskStatus.FAILED
                and not self._failed_task_is_historical(task, completed_external_refs, completed_review_dimensions)
            ],
        }

    def _repair_evidence_manifest(
        self,
        *,
        signal: dict[str, object],
        target_task: Task | None,
        target_objective,
        classification: str | None,
        failure_family: str,
        write_probe: dict[str, object],
        repair_memory: list[dict[str, object]],
    ) -> dict[str, object]:
        manifest = self._evidence_manifest(signal)
        manifest["repair_context"] = {
            "classification": classification,
            "failure_family": failure_family,
            "target_task_id": target_task.id if target_task is not None else None,
            "target_objective_id": target_objective.id if target_objective is not None else None,
            "workspace_write_probe": write_probe,
            "related_repair_memory": repair_memory,
        }
        return manifest

    def _latest_classification_for_task(self, task_id: str) -> str | None:
        for run in self.store.list_control_worker_runs(task_id=task_id):
            if run.classification:
                return run.classification
        return None

    def _default_repair_title(self, task: Task | None, objective, classification: str | None) -> str:
        if task is not None:
            return f"Repair harness workflow blocking {task.title}"
        return f"Repair harness workflow blocking {objective.title}"

    def _default_repair_objective(self, task: Task | None, objective, classification: str | None, *, manifest_path: Path) -> str:
        classification_text = classification or "structural failure"
        subject = f"task '{task.title}'" if task is not None else f"objective '{objective.title}'"
        return (
            "You are sa-watch repairing the Accruvia harness itself.\n"
            f"The current pipeline is blocked around {subject} because of {classification_text}.\n"
            "Inspect the harness codebase and make the architectural, workflow, or control-plane change directly.\n"
            "Do not create product work. Do not stop at a band-aid restart. Fix the machine.\n"
            f"Read the full evidence manifest first: {manifest_path}\n"
            "Required proof:\n"
            "- inspect prior related repair artifacts and repeated failure evidence before changing code\n"
            "- do not claim workspace blockage unless the manifest shows the write probe failed\n"
            "- do not repeat a repair class that already produced zero durable changes\n"
            "- identify the root cause precisely in the repair evidence\n"
            "- record a blameless six-whys review grounded in concrete evidence; if you cannot support a deeper why, say what evidence is missing\n"
            "- implement the durable harness change\n"
            "- validate the repaired path locally\n"
            "- leave durable evidence describing what changed and why tasks should move again\n"
        )

    def _repair_artifact_inventory(self, repair_result: SAWatchRepairResult) -> list[str]:
        run_dir = repair_result.run_dir
        if not run_dir.exists():
            return []
        artifacts: list[str] = []
        for path in sorted(run_dir.iterdir()):
            if path.is_file():
                artifacts.append(path.name)
        return artifacts

    def _blameless_six_whys_review(
        self,
        *,
        decision: SAWatchDecision,
        signal: dict[str, object],
        repair_result: SAWatchRepairResult,
        movement_restored: bool,
    ) -> dict[str, object]:
        signal_kind = str(signal.get("kind") or "structural_stall")
        failure_message = str(
            repair_result.diagnostics.get("failure_message")
            or repair_result.diagnostics.get("error")
            or repair_result.summary
            or "No detailed repair failure message was recorded."
        ).strip()
        artifact_inventory = self._repair_artifact_inventory(repair_result)
        validation = repair_result.validation if isinstance(repair_result.validation, dict) else {}
        compile_ok = bool((validation.get("compile_check") or {}).get("ok")) if isinstance(validation.get("compile_check"), dict) else False
        test_ok = bool((validation.get("test_check") or {}).get("ok")) if isinstance(validation.get("test_check"), dict) else False
        known_facts = [
            f"sa-watch targeted signal '{signal_kind}'.",
            f"Repair run status was '{repair_result.status}'.",
            f"Changed files recorded: {', '.join(repair_result.changed_files) if repair_result.changed_files else 'none'}.",
            f"Validation status: compile_ok={compile_ok}, test_ok={test_ok}.",
            f"Movement restored after repair: {movement_restored}.",
        ]
        if artifact_inventory:
            known_facts.append(f"Repair artifacts present: {', '.join(artifact_inventory)}.")
        why_chain = [
            {
                "level": 1,
                "question": "Why did the pipeline stop moving?",
                "answer": f"sa-watch observed the continuity signal '{signal_kind}' and treated it as a structural workflow risk.",
                "evidence": ["signal.kind", "decision.reason"],
            },
            {
                "level": 2,
                "question": "Why did sa-watch choose a harness repair instead of waiting or restarting?",
                "answer": decision.reason or "The decision packet selected direct harness repair.",
                "evidence": ["decision.reason", "decision.action"],
            },
            {
                "level": 3,
                "question": "Why did the attempted repair succeed or fail to restore movement?",
                "answer": (
                    "The repair produced enough validated evidence for forward progress to resume."
                    if movement_restored
                    else f"The repair did not provide enough validated evidence to show movement resumed. Latest repair result: {failure_message}"
                ),
                "evidence": ["repair_result.summary", "repair_result.validation", "movement_validation"],
            },
            {
                "level": 4,
                "question": "Why was that gap not resolved within the repair attempt itself?",
                "answer": (
                    "The repair scope appears to have addressed the immediate workflow defect but did not fully prove end-to-end movement."
                    if repair_result.changed_files
                    else "The repair attempt did not record durable code changes, so the structural hypothesis could not be fully validated."
                ),
                "evidence": ["changed_files", "validation"],
            },
            {
                "level": 5,
                "question": "Why could operators misread the real issue from the run evidence?",
                "answer": (
                    "The artifact set is generic unless the repair explicitly records a structured review. Without that, operators mostly see summary strings and validation outputs."
                ),
                "evidence": ["repair_evidence.summary", "repair_artifact_inventory"],
            },
            {
                "level": 6,
                "question": "Why do repeated incidents require deeper questioning?",
                "answer": (
                    "Because shallow summaries collapse environment, workflow, and validation issues together. A fixed six-whys structure forces the investigation to separate observed facts, contributing factors, and unresolved unknowns."
                ),
                "evidence": ["blameless_review", "diagnostics", "validation"],
            },
        ]
        return {
            "method": "six_whys",
            "blameless": True,
            "known_facts": known_facts,
            "evidence_reviewed": artifact_inventory,
            "contributing_factors": [
                "continuity signal required structural interpretation",
                "repair quality depended on durable evidence and local validation",
                "operator diagnosis quality depends on artifact richness, not just terminal status",
            ],
            "why_chain": why_chain,
            "unknowns": [] if movement_restored else [
                "Whether the underlying workflow defect is fully removed from all similar objectives.",
                "Whether the repair evidence captured enough detail for future operators without re-reading raw artifacts.",
            ],
            "next_questions": [
                "What exact artifact or trace would have made the causal chain obvious without manual probing?",
                "What validator or guardrail could reject this failure mode earlier?",
                "Which similar repair tasks should inherit the same context and questioning pattern?",
            ],
        }

    def _target_objective_for_signal(self, structural_signal: dict[str, object] | None) -> object | None:
        if not structural_signal:
            return None
        objective_id = str(structural_signal.get("objective_id") or "")
        if not objective_id:
            task_id = str(structural_signal.get("task_id") or "")
            if not task_id:
                return None
            task = self.store.get_task(task_id)
            objective_id = str(task.objective_id or "") if task is not None else ""
        if not objective_id:
            return None
        return self.store.get_objective(objective_id)

    def _stale_atomic_generation_signal(self) -> dict[str, object] | None:
        cutoff = datetime.now(UTC) - timedelta(minutes=5)
        for objective in self.store.list_objectives():
            latest_atomic = self._latest_atomic_generation_state(objective.id)
            if latest_atomic is None:
                continue
            if latest_atomic.get("status") != "running" or not latest_atomic.get("is_stale"):
                continue
            linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
            if any(task.status in {TaskStatus.PENDING, TaskStatus.ACTIVE} for task in linked_tasks):
                continue
            if latest_atomic["last_activity_at"] > cutoff:
                continue
            return {
                "kind": "stale_atomic_generation",
                "objective_id": objective.id,
                "generation_id": latest_atomic.get("generation_id"),
                "phase": latest_atomic.get("phase"),
                "last_activity_at": self._local_time(latest_atomic.get("last_activity_at")),
            }
        return None

    def _latest_atomic_generation_state(self, objective_id: str) -> dict[str, object] | None:
        starts = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_started")
        ]
        if not starts:
            return None
        start = starts[-1]
        generation_id = str(start.metadata.get("generation_id") or start.id)
        progress = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_progress")
            if str(record.metadata.get("generation_id") or "") == generation_id
        ]
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
        status = "running"
        if completed is not None:
            status = "completed"
        elif failed is not None:
            status = "failed"
        related_times = [start.created_at]
        related_times.extend(record.created_at for record in progress)
        if completed is not None:
            related_times.append(completed.created_at)
        if failed is not None:
            related_times.append(failed.created_at)
        last_activity = max(related_times)
        phase = str(progress[-1].metadata.get("phase") or "") if progress else ""
        return {
            "generation_id": generation_id,
            "status": status,
            "phase": phase,
            "last_activity_at": last_activity,
            "is_stale": status == "running" and (datetime.now(UTC) - last_activity) > timedelta(minutes=5),
        }

    def _run_direct_repair(self, repair_task: Task, repair_run: Run, repo_root: Path) -> SAWatchRepairResult:
        run_dir = self.workspace_root / "control" / "sa_watch_repairs" / repair_run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        if self.structural_progress_callback is not None:
            self.structural_progress_callback(
                {
                    "type": "run_created",
                    "task_id": repair_task.id,
                    "run_id": repair_run.id,
                    "attempt": repair_run.attempt,
                }
            )
        env = {
            "ACCRUVIA_RUN_DIR": str(run_dir),
            "ACCRUVIA_PROJECT_WORKSPACE": str(repo_root),
            "ACCRUVIA_TASK_ID": repair_task.id,
            "ACCRUVIA_RUN_ID": repair_run.id,
            "ACCRUVIA_RUN_ATTEMPT": str(repair_run.attempt),
            "ACCRUVIA_TASK_TITLE": repair_task.title,
            "ACCRUVIA_TASK_OBJECTIVE": repair_task.objective,
            "ACCRUVIA_RUN_SUMMARY": repair_run.summary,
            "ACCRUVIA_TASK_SCOPE_JSON": json.dumps(repair_task.scope, sort_keys=True),
            "ACCRUVIA_TASK_EXTERNAL_METADATA_JSON": json.dumps(repair_task.external_ref_metadata, sort_keys=True),
            "ACCRUVIA_TASK_REQUIRED_ARTIFACTS": json.dumps(repair_task.required_artifacts, sort_keys=True),
            "ACCRUVIA_TASK_STRATEGY": repair_task.strategy,
            "ACCRUVIA_TASK_VALIDATION_PROFILE": repair_task.validation_profile,
            "ACCRUVIA_TASK_VALIDATION_MODE": repair_task.validation_mode,
        }
        agent_exit = run_agent_worker(env)
        report_path = run_dir / "report.json"
        report = self._read_json_dict(report_path)
        if agent_exit == 0 and str(report.get("worker_outcome") or "") == "candidate":
            run_validation(env)
            report = self._read_json_dict(report_path)
        compile_check = report.get("compile_check") if isinstance(report.get("compile_check"), dict) else {}
        test_check = report.get("test_check") if isinstance(report.get("test_check"), dict) else {}
        validated = (
            str(report.get("worker_outcome") or "") == "success"
            and bool(compile_check.get("ok"))
            and bool(test_check.get("ok"))
        )
        stdout_summary = None
        stdout_path = run_dir / "codex_worker.stdout.txt"
        if stdout_path.exists():
            stdout_summary = next((line.strip() for line in stdout_path.read_text(encoding="utf-8").splitlines() if line.strip()), None)
        return SAWatchRepairResult(
            status="validated" if validated else ("blocked" if report.get("blocked") else "failed"),
            run_id=repair_run.id,
            run_dir=run_dir,
            summary=str(report.get("failure_message") or stdout_summary or repair_run.summary),
            changed_files=[str(item) for item in report.get("changed_files", []) if str(item).strip()],
            validation={
                "compile_check": compile_check,
                "test_check": test_check,
                "validation_elapsed_seconds": report.get("validation_elapsed_seconds"),
            },
            diagnostics=report,
            stdout_summary=stdout_summary,
        )

    def _record_repair_evidence(
        self,
        *,
        repair_task: Task,
        repair_run: Run,
        decision: SAWatchDecision,
        signal: dict[str, object],
        repair_result: SAWatchRepairResult,
        before_progress: dict[str, object],
        after_progress: dict[str, object],
        movement_restored: bool,
    ) -> None:
        evidence_path = repair_result.run_dir / "repair_evidence.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence = {
            "repair_run_id": repair_run.id,
            "root_cause": decision.reason,
            "repair_title": repair_task.title,
            "repair_objective": repair_task.objective,
            "summary": repair_result.summary,
            "changed_files": repair_result.changed_files,
            "validation": repair_result.validation,
            "diagnostics": repair_result.diagnostics,
            "repair_artifact_inventory": self._repair_artifact_inventory(repair_result),
            "blameless_review": self._blameless_six_whys_review(
                decision=decision,
                signal=signal,
                repair_result=repair_result,
                movement_restored=movement_restored,
            ),
            "signal": signal,
            "repair_memory": {
                "failure_family": str(
                    ((repair_task.external_ref_metadata or {}).get("sa_watch_context") or {}).get("failure_family") or ""
                ),
                "signal_kind": str(signal.get("kind") or "structural_stall"),
                "root_cause_hypothesis": decision.reason,
                "action_class": "repair_harness",
                "changed_files_count": len(repair_result.changed_files),
                "validation_result": repair_result.status,
                "movement_result": "restored" if movement_restored else "not_restored",
                "durable_repair": self._repair_is_durable(repair_result),
            },
            "movement_validation": {
                "before": before_progress,
                "after": after_progress,
                "movement_restored": movement_restored,
            },
            "why_pipeline_can_move_again": (
                "Forward-progress indicators improved after a durable repair."
                if movement_restored
                else "Local validation did not provide enough evidence that a durable repair restored pipeline movement."
            ),
        }
        evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="sa_watch_repair",
                project_id=repair_task.project_id,
                objective_id=repair_task.objective_id,
                visibility="operator_visible",
                author_type="system",
                author_id="sa-watch",
                content=f"sa-watch direct repair recorded at {evidence_path}",
                metadata=evidence,
            )
        )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="system",
                entity_id="system",
                event_type="sa_watch_repair_recorded",
                payload={
                    "repair_run_id": repair_run.id,
                    "objective_id": repair_task.objective_id,
                    "evidence_path": str(evidence_path),
                    "movement_restored": movement_restored,
                },
            )
        )

    def _read_json_dict(self, path: Path) -> dict[str, object]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}
