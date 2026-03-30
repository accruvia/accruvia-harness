from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .control_plane import ControlPlane
from .domain import (
    ContextRecord,
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
from .store import SQLiteHarnessStore
from .services.task_service import TaskService

if TYPE_CHECKING:
    from .engine import HarnessEngine


# Run periodically as a continuity supervisor rather than a hot-path recovery
# hook. The goal is to insist on continued forward motion without thrashing.
SA_WATCH_INTERVAL_SECONDS = 1200
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


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
    ) -> None:
        self.store = store
        self.control_plane = control_plane
        self.llm_router = llm_router
        self.workspace_root = workspace_root
        self.interval_seconds = interval_seconds
        self._last_invoked_at = 0.0
        self.tasks = TaskService(store)
        self.engine = engine
        self.structural_progress_callback = structural_progress_callback
        self.post_repair_callback = post_repair_callback
        self.restart_stack = restart_stack

    def observe(self, event: dict[str, object]) -> dict[str, object] | None:
        if str(event.get("type") or "") != "sleeping":
            return None
        if time.monotonic() - self._last_invoked_at < self.interval_seconds:
            return None
        self._last_invoked_at = time.monotonic()
        return self.run_once()

    def run_once(self) -> dict[str, object]:
        packet = self._build_packet()
        in_progress = self._recovery_in_progress(packet)
        if in_progress is not None:
            return in_progress
        if self.llm_router is None or not getattr(self.llm_router, "executors", {}):
            return self._record_skip("llm_router_unavailable", packet)
        try:
            decision = self._invoke(packet)
        except (LLMExecutionError, ValueError, json.JSONDecodeError) as exc:
            return self._record_skip(f"llm_execution_failed:{exc}", packet)
        return self._apply(decision, packet)

    def _structural_signal(self) -> dict[str, object] | None:
        signals = self._continuity_signals()
        return signals[0] if signals else None

    def _build_packet(self) -> dict[str, object]:
        status = self.control_plane.status()
        continuity_signals = self._continuity_signals()
        structural_signal = continuity_signals[0] if continuity_signals else None
        recent_events = [
            {
                "event_type": event.event_type,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
            }
            for event in self.store.list_control_events(limit=8)
        ]
        recent_runs = [
            {
                "run_id": item.id,
                "task_id": item.task_id,
                "status": item.status,
                "classification": item.classification,
                "started_at": item.started_at.isoformat(),
                "ended_at": item.ended_at.isoformat() if item.ended_at else None,
            }
            for item in self.store.list_control_worker_runs()[:5]
        ]
        recent_actions = [
            {
                "action_type": item.action_type,
                "target_type": item.target_type,
                "target_id": item.target_id,
                "reason": item.reason,
                "result": item.result,
                "created_at": item.created_at.isoformat(),
            }
            for item in self.store.list_control_recovery_actions()[:5]
        ]
        objective_summaries = self._objective_summaries()
        task_summary = self._task_summary()
        return {
            "status": status,
            "continuity_goal": "Work should keep moving. Detect loops, stalls, and dead workflow states, then restore forward progress safely.",
            "continuity_signals": continuity_signals,
            "structural_signal": structural_signal,
            "target_task": self._target_task_packet(structural_signal),
            "target_objective": self._target_objective_packet(structural_signal),
            "target_task_evidence": self._target_task_evidence(structural_signal),
            "target_objective_evidence": self._target_objective_evidence(structural_signal),
            "objective_summaries": objective_summaries,
            "task_summary": task_summary,
            "recent_events": recent_events,
            "recent_worker_runs": recent_runs,
            "recent_recovery_actions": recent_actions,
            "allowed_actions": [
                "none",
                "resume_worker",
                "restart_stack",
                "freeze_system",
                "record_escalation",
                "create_corrective_task",
            ],
        }

    def _build_prompt(self, packet: dict[str, object]) -> str:
        return (
            "You are sa-watch for the Accruvia harness control plane.\n"
            "Your job is to keep work moving.\n"
            "You are the periodic continuity supervisor for loops, stalls, and dead workflows.\n"
            "Read the durable control-plane state, identify whether forward progress is blocked, and choose the safest action that restores momentum.\n"
            "The control plane already handles routine hot-path checks. You are here to recover the big picture when work is looping, paused too long, or no longer advancing.\n"
            "You must read the structured state below and choose exactly one action.\n\n"
            "Rules:\n"
            "- Work should not stay stopped without a strong reason.\n"
            "- Prefer the cheapest safe action that restores forward progress.\n"
            "- Prefer resume_worker or restart_stack before creating new work when existing work can likely continue.\n"
            "- Use create_corrective_task when the current workflow is looping or structurally stuck and needs a real code or workflow fix.\n"
            "- A corrective task must target the source of the stall or loop, not just ask for another report.\n"
            "- Freeze only when continuing would be unsafe or clearly runaway.\n"
            "- Escalate only when you cannot safely restore momentum from the available evidence.\n"
            "- Output JSON only. No markdown, no prose outside the JSON object.\n\n"
            "Allowed actions:\n"
            '- "none"\n'
            '- "resume_worker"\n'
            '- "restart_stack"\n'
            '- "freeze_system"\n'
            '- "record_escalation"\n\n'
            '- "create_corrective_task"\n\n'
            "Return this exact schema:\n"
            '{\n'
            '  "action": "one allowed action",\n'
            '  "reason": "short factual explanation tied to the evidence",\n'
            '  "confidence": 0.0,\n'
            '  "target_lane": "worker|harness|null",\n'
            '  "target_task_id": "task id or null",\n'
            '  "task_title": "required when action=create_corrective_task",\n'
            '  "task_objective": "required when action=create_corrective_task; must specify the structural fix and proof required",\n'
            '  "escalate": true\n'
            '}\n\n'
            "Current packet:\n"
            f"{json.dumps(packet, indent=2, sort_keys=True)}\n"
        )

    def _invoke(self, packet: dict[str, object]) -> SAWatchDecision:
        task = Task(
            id=new_id("task"),
            project_id="system",
            title="sa-watch intervention review",
            objective="Review degraded control-plane state and pick one bounded intervention.",
            status=TaskStatus.ACTIVE,
            strategy="sa_watch",
        )
        run = Run(
            id=new_id("run"),
            task_id=task.id,
            status=RunStatus.PLANNING,
            attempt=1,
            summary="sa-watch review",
        )
        run_dir = self.workspace_root / "control" / "sa_watch" / run.id
        result, _backend = self.llm_router.execute(
            LLMInvocation(task=task, run=run, prompt=self._build_prompt(packet), run_dir=run_dir)
        )
        parsed = self._parse_decision(result.response_text)
        return SAWatchDecision(
            action=str(parsed.get("action") or "record_escalation"),
            reason=str(parsed.get("reason") or "sa-watch returned no reason"),
            confidence=float(parsed.get("confidence") or 0.0),
            target_lane=str(parsed.get("target_lane")) if parsed.get("target_lane") is not None else None,
            escalate=bool(parsed.get("escalate")),
            task_title=str(parsed.get("task_title") or "") or None,
            task_objective=str(parsed.get("task_objective") or "") or None,
            target_task_id=str(parsed.get("target_task_id") or "") or None,
        )

    def _parse_decision(self, response_text: str) -> dict[str, Any]:
        stripped = response_text.strip()
        if stripped.startswith("{"):
            return json.loads(stripped)
        match = _JSON_BLOCK_RE.search(stripped)
        if match:
            return json.loads(match.group(1))
        raise ValueError("sa-watch response did not contain a JSON object")

    def _apply(self, decision: SAWatchDecision, packet: dict[str, object]) -> dict[str, object]:
        action = decision.action
        effects: list[dict[str, object]] = []
        if not self._usable_reason(decision.reason):
            fallback = self._deterministic_unstall(packet)
            if fallback is not None:
                return fallback
            status = self.control_plane.status()
            self._record_action("model_response_unusable", "system", "system", decision.reason, "recorded")
            effects.append({"kind": "model_response_unusable", "reason": decision.reason})
            return {
                "decision": {
                    "action": "model_response_unusable",
                    "reason": decision.reason,
                    "confidence": decision.confidence,
                    "target_lane": decision.target_lane,
                    "target_task_id": decision.target_task_id,
                    "task_title": decision.task_title,
                    "escalate": False,
                },
                "status": status,
                "packet": packet,
                "effects": effects,
            }
        if action == "resume_worker":
            status = self.control_plane.resume_lane("worker", reason=f"sa_watch:{decision.reason}")
            status = self.control_plane.mark_healthy(reason="sa_watch_resumed_worker")
            self._record_action("resume", "lane", "worker", decision.reason, "applied")
            effects.append({"kind": "lane_resumed", "lane": "worker", "reason": decision.reason})
        elif action == "restart_stack":
            status = self._restart_stack(decision)
            effects.append({"kind": "stack_restart_requested", "reason": decision.reason})
        elif action == "freeze_system":
            status = self.control_plane.freeze(f"sa_watch:{decision.reason}")
            self._record_action("freeze", "system", "system", decision.reason, "applied")
            effects.append({"kind": "system_frozen", "reason": decision.reason})
        elif action == "create_corrective_task":
            status, created_effects = self._create_corrective_task(decision, packet)
            effects.extend(created_effects)
        elif action in {"record_escalation", "none"}:
            status = self.control_plane.status()
            self._record_action(
                "escalate" if action == "record_escalation" or decision.escalate else "observe",
                "system",
                "system",
                decision.reason,
                "recorded",
            )
            effects.append(
                {
                    "kind": "noted_concern" if action == "record_escalation" or decision.escalate else "observed",
                    "reason": decision.reason,
                }
            )
        else:
            status = self.control_plane.status()
            self._record_action("invalid_action", "system", "system", f"{action}:{decision.reason}", "ignored")
            effects.append({"kind": "invalid_action", "action": action, "reason": decision.reason})
        return {
            "decision": {
                "action": decision.action,
                "reason": decision.reason,
                "confidence": decision.confidence,
                "target_lane": decision.target_lane,
                "target_task_id": decision.target_task_id,
                "task_title": decision.task_title,
                "escalate": decision.escalate,
            },
            "status": status,
            "packet": packet,
            "effects": effects,
        }

    def _record_skip(self, reason: str, packet: dict[str, object]) -> dict[str, object]:
        self._record_action("skip", "system", "system", reason, "recorded")
        return {"decision": {"action": "skip", "reason": reason}, "status": self.control_plane.status(), "packet": packet}

    def _usable_reason(self, reason: str) -> bool:
        return reason.strip().lower() not in {"", "sa-watch returned no reason"}

    def _recovery_in_progress(self, packet: dict[str, object]) -> dict[str, object] | None:
        task_summary = dict(packet.get("task_summary") or {})
        if int(task_summary.get("sa_structural_fix_active", 0) or 0) <= 0:
            return None
        reason = "structural_fix_in_progress"
        self._record_action("observe", "system", "system", reason, "recorded")
        return {
            "decision": {
                "action": "none",
                "reason": reason,
                "confidence": 1.0,
                "target_lane": None,
                "target_task_id": None,
                "task_title": None,
                "escalate": False,
            },
            "status": self.control_plane.status(),
            "packet": packet,
            "effects": [
                {
                    "kind": "observed",
                    "reason": reason,
                }
            ],
        }

    def _deterministic_unstall(self, packet: dict[str, object]) -> dict[str, object] | None:
        task_summary = dict(packet.get("task_summary") or {})
        signals = list(packet.get("continuity_signals") or [])
        signal_kinds = {str(signal.get("kind") or "") for signal in signals}
        if task_summary.get("sa_structural_fix_pending", 0) <= 0:
            return None
        if task_summary.get("active", 0) > 0:
            return None
        if not signal_kinds.intersection({"objective_stalled", "no_progress", "workflow_gap", "worker_paused"}):
            return None
        reason = "deterministic_unstall_pending_structural_fix"
        status = self.control_plane.resume_lane("worker", reason=reason)
        status = self.control_plane.mark_healthy(reason=reason)
        self._record_action("resume", "lane", "worker", reason, "applied")
        return {
            "decision": {
                "action": "resume_worker",
                "reason": reason,
                "confidence": 1.0,
                "target_lane": "worker",
                "target_task_id": None,
                "task_title": None,
                "escalate": False,
            },
            "status": status,
            "packet": packet,
            "effects": [
                {
                    "kind": "lane_resumed",
                    "lane": "worker",
                    "reason": reason,
                }
            ],
        }

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

    def _create_corrective_task(self, decision: SAWatchDecision, packet: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
        target_task = self.store.get_task(str(decision.target_task_id or ""))
        if target_task is None:
            signal = packet.get("structural_signal") or {}
            target_task = self.store.get_task(str(signal.get("task_id") or ""))
        target_objective = self._target_objective_for_signal(packet.get("structural_signal") or {})
        if target_task is None and target_objective is None:
            self._record_action("escalate", "system", "system", f"missing_target:{decision.reason}", "recorded")
            return self.control_plane.status(), [{"kind": "noted_concern", "reason": f"missing_target:{decision.reason}"}]
        project = self.store.get_project(
            target_task.project_id if target_task is not None else str(target_objective.project_id)
        )
        if project is None:
            self._record_action("escalate", "system", "system", f"missing_project:{decision.reason}", "recorded")
            return self.control_plane.status(), [{"kind": "noted_concern", "reason": f"missing_project:{decision.reason}"}]
        classification = (
            self._latest_classification_for_task(target_task.id)
            if target_task is not None
            else str((packet.get("structural_signal") or {}).get("kind") or "structural_stall")
        )
        corrective_ref_id = (
            self._next_corrective_ref_id(target_task.id, classification)
            if target_task is not None
            else self._next_objective_corrective_ref_id(target_objective.id, classification)
        )
        title = (
            decision.task_title
            or (
                f"Prevent recurrence of {classification or 'structural'} failure in {target_task.title}"
                if target_task is not None
                else f"Unblock stalled objective workflow for {target_objective.title}"
            )
        )
        objective = (
            decision.task_objective
            or (
                self._default_corrective_objective(target_task, classification)
                if target_task is not None
                else self._default_objective_corrective_objective(target_objective, classification)
            )
        )
        corrective_task = self.tasks.create_task_with_policy(
            project_id=project.id,
            objective_id=target_task.objective_id if target_task is not None else target_objective.id,
            title=title,
            objective=objective,
            priority=max(150, int(target_task.priority if target_task is not None else target_objective.priority)),
            parent_task_id=target_task.id if target_task is not None else None,
            source_run_id=None,
            external_ref_type="sa_watch",
            external_ref_id=corrective_ref_id,
            external_ref_metadata={
                "sa_watch": {
                    "source_task_id": target_task.id if target_task is not None else None,
                    "source_objective_id": target_task.objective_id if target_task is not None else target_objective.id,
                    "classification": classification,
                    "corrective_ref_id": corrective_ref_id,
                    "trigger": packet.get("structural_signal"),
                    "reason": decision.reason,
                }
            },
            validation_profile=target_task.validation_profile if target_task is not None else "generic",
            validation_mode=target_task.validation_mode if target_task is not None else "lightweight_operator",
            scope=dict(target_task.scope) if target_task is not None else {},
            strategy="sa_structural_fix",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        objective_record = self.store.get_objective(
            target_task.objective_id if target_task is not None else target_objective.id
        )
        if objective_record is not None and objective_record.status == ObjectiveStatus.PAUSED:
            self.store.update_objective_status(objective_record.id, ObjectiveStatus.PLANNING)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="sa_watch_action",
                project_id=project.id,
                objective_id=target_task.objective_id if target_task is not None else target_objective.id,
                task_id=corrective_task.id,
                visibility="operator_visible",
                author_type="system",
                author_id="sa-watch",
                content=f"sa-watch created corrective task {corrective_task.title}",
                metadata={
                    "source_task_id": target_task.id if target_task is not None else None,
                    "source_objective_id": target_task.objective_id if target_task is not None else target_objective.id,
                    "classification": classification,
                    "reason": decision.reason,
                },
            )
        )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=corrective_task.id,
                event_type="sa_watch_corrective_task_created",
                payload={
                    "source_task_id": target_task.id if target_task is not None else None,
                    "source_objective_id": target_task.objective_id if target_task is not None else target_objective.id,
                    "classification": classification,
                    "reason": decision.reason,
                },
            )
        )
        self._record_action("create_corrective_task", "task", corrective_task.id, decision.reason, "applied")
        effects = [
            {
                "kind": "corrective_task_created",
                "task_id": corrective_task.id,
                "title": corrective_task.title,
                "objective_id": corrective_task.objective_id,
                "classification": classification,
            }
        ]
        if self.engine is not None:
            status, execution_effects = self._execute_structural_fix(corrective_task, packet, decision)
            effects.extend(execution_effects)
            return status, effects
        return self.control_plane.status(), effects

    def _execute_structural_fix(
        self,
        corrective_task: Task,
        packet: dict[str, object],
        decision: SAWatchDecision,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        signal = packet.get("structural_signal") or {}
        before_progress = self._progress_snapshot(signal)
        effects: list[dict[str, object]] = []
        try:
            self.engine.run_until_stable(
                corrective_task.id,
                progress_callback=self.structural_progress_callback,
                post_task_callback=self.post_repair_callback,
            )
        except Exception as exc:
            self._record_action("hot_patch", "task", corrective_task.id, f"{decision.reason}:execute_failed:{exc}", "failed")
            self.control_plane.mark_degraded("sa_watch_hot_patch_failed")
            effects.append({"kind": "hot_patch_failed", "task_id": corrective_task.id, "reason": str(exc)})
            return self.control_plane.status(), effects
        refreshed = self.store.get_task(corrective_task.id)
        if refreshed is None:
            self._record_action("hot_patch", "task", corrective_task.id, f"{decision.reason}:missing_after_execution", "failed")
            self.control_plane.mark_degraded("sa_watch_hot_patch_failed")
            effects.append({"kind": "hot_patch_failed", "task_id": corrective_task.id, "reason": "missing_after_execution"})
            return self.control_plane.status(), effects
        if refreshed.status != TaskStatus.COMPLETED:
            self._record_action("hot_patch", "task", refreshed.id, decision.reason, "failed")
            self.control_plane.mark_degraded("sa_watch_hot_patch_failed")
            effects.append({"kind": "hot_patch_failed", "task_id": refreshed.id, "reason": refreshed.status.value})
            return self.control_plane.status(), effects
        restart_status = None
        if self.restart_stack is not None:
            restart_status = self.restart_stack(
                {
                    "reason": "sa_watch_hot_patch_completed",
                    "task_id": refreshed.id,
                    "objective_id": refreshed.objective_id,
                }
            )
            effects.append({"kind": "stack_restart_requested", "task_id": refreshed.id, "reason": "sa_watch_hot_patch_completed"})
        if not self._forward_progress_resumed(signal, before_progress):
            self._record_action("hot_patch", "task", refreshed.id, decision.reason, "verification_failed")
            self.control_plane.freeze(f"sa_watch_no_forward_progress:{decision.reason}")
            effects.append({"kind": "system_frozen", "reason": f"sa_watch_no_forward_progress:{decision.reason}"})
            return restart_status or self.control_plane.status(), effects
        self._record_action("hot_patch", "task", refreshed.id, decision.reason, "verified")
        self.control_plane.resume_lane("worker", reason="sa_watch_hot_patch_completed")
        self.control_plane.mark_healthy(reason="sa_watch_hot_patch_completed")
        effects.append({"kind": "hot_patch_verified", "task_id": refreshed.id})
        effects.append({"kind": "lane_resumed", "lane": "worker", "reason": "sa_watch_hot_patch_completed"})
        return restart_status or self.control_plane.status(), effects

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
        return {
            "linked_task_counts": {
                "pending": sum(1 for task in linked_tasks if task.status == TaskStatus.PENDING),
                "active": sum(1 for task in linked_tasks if task.status == TaskStatus.ACTIVE),
                "completed": sum(1 for task in linked_tasks if task.status == TaskStatus.COMPLETED),
                "failed": sum(1 for task in linked_tasks if task.status == TaskStatus.FAILED),
            },
            "latest_atomic_generation": {
                **latest_atomic,
                "last_activity_at": latest_atomic["last_activity_at"].isoformat() if latest_atomic is not None else None,
            }
            if latest_atomic is not None
            else None,
        }

    def _progress_snapshot(self, structural_signal: dict[str, object] | None) -> dict[str, object]:
        objective = self._target_objective_for_signal(structural_signal)
        if objective is None:
            return {"objective_id": None}
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
        return {
            "objective_id": objective.id,
            "status": objective.status.value,
            "pending": sum(1 for task in linked_tasks if task.status == TaskStatus.PENDING),
            "active": sum(1 for task in linked_tasks if task.status == TaskStatus.ACTIVE),
            "completed": sum(1 for task in linked_tasks if task.status == TaskStatus.COMPLETED),
            "failed": sum(1 for task in linked_tasks if task.status == TaskStatus.FAILED),
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
        if before.get("status") != after.get("status") and after.get("status") in {
            ObjectiveStatus.PLANNING.value,
            ObjectiveStatus.EXECUTING.value,
        }:
            return True
        return False

    def _continuity_signals(self) -> list[dict[str, object]]:
        signals: list[dict[str, object]] = []
        stale_atomic = self._stale_atomic_generation_signal()
        if stale_atomic is not None:
            signals.append(stale_atomic)
        repeated_failure = self._repeated_failure_signal()
        if repeated_failure is not None:
            signals.append(repeated_failure)
        no_progress = self._no_progress_signal()
        if no_progress is not None:
            signals.append(no_progress)
        objective_stalled = self._objective_stalled_signal()
        if objective_stalled is not None:
            signals.append(objective_stalled)
        worker_paused = self._worker_paused_signal()
        if worker_paused is not None:
            signals.append(worker_paused)
        workflow_gap = self._workflow_gap_signal()
        if workflow_gap is not None:
            signals.append(workflow_gap)
        return signals

    def _repeated_failure_signal(self) -> dict[str, object] | None:
        recent_runs = self.store.list_control_worker_runs()[:8]
        grouped: dict[tuple[str, str], list[object]] = {}
        for run in recent_runs:
            if not run.task_id or not run.classification:
                continue
            grouped.setdefault((run.task_id, run.classification), []).append(run)
        for (task_id, classification), runs in grouped.items():
            if len(runs) >= 2:
                return {
                    "kind": "repeated_failure",
                    "task_id": task_id,
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

    def _objective_stalled_signal(self) -> dict[str, object] | None:
        stalled = self.store.list_control_events(event_type="objective_stalled", limit=8)
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
        active_structural = [
            task
            for task in self.store.list_tasks()
            if task.status in {TaskStatus.PENDING, TaskStatus.ACTIVE} and str(task.strategy or "") == "sa_structural_fix"
        ]
        return {
            "kind": "worker_paused",
            "lane_reason": lane.reason,
            "active_structural_fix_count": len(active_structural),
        }

    def _workflow_gap_signal(self) -> dict[str, object] | None:
        unresolved = [objective for objective in self.store.list_objectives() if objective.status != ObjectiveStatus.RESOLVED]
        if not unresolved:
            return None
        pending_or_active = [
            task
            for task in self.store.list_tasks()
            if task.status in {TaskStatus.PENDING, TaskStatus.ACTIVE}
        ]
        if pending_or_active:
            return None
        oldest = sorted(unresolved, key=lambda item: item.updated_at)[0]
        latest_atomic = self._latest_atomic_generation_state(oldest.id)
        return {
            "kind": "workflow_gap",
            "objective_id": oldest.id,
            "objective_status": oldest.status.value,
            "last_activity_at": oldest.updated_at.isoformat(),
            "latest_atomic_generation": latest_atomic["status"] if latest_atomic is not None else None,
        }

    def _objective_summaries(self) -> list[dict[str, object]]:
        summaries: list[dict[str, object]] = []
        for objective in self.store.list_objectives()[:5]:
            linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
            latest_atomic = self._latest_atomic_generation_state(objective.id)
            summaries.append(
                {
                    "objective_id": objective.id,
                    "title": objective.title,
                    "status": objective.status.value,
                    "updated_at": objective.updated_at.isoformat(),
                    "pending_tasks": sum(1 for task in linked_tasks if task.status == TaskStatus.PENDING),
                    "active_tasks": sum(1 for task in linked_tasks if task.status == TaskStatus.ACTIVE),
                    "completed_tasks": sum(1 for task in linked_tasks if task.status == TaskStatus.COMPLETED),
                    "failed_tasks": sum(1 for task in linked_tasks if task.status == TaskStatus.FAILED),
                    "latest_atomic_generation": {
                        **latest_atomic,
                        "last_activity_at": latest_atomic["last_activity_at"].isoformat() if latest_atomic is not None else None,
                    }
                    if latest_atomic is not None
                    else None,
                }
            )
        return summaries

    def _task_summary(self) -> dict[str, int]:
        tasks = self.store.list_tasks()
        return {
            "pending": sum(1 for task in tasks if task.status == TaskStatus.PENDING),
            "active": sum(1 for task in tasks if task.status == TaskStatus.ACTIVE),
            "completed": sum(1 for task in tasks if task.status == TaskStatus.COMPLETED),
            "failed": sum(1 for task in tasks if task.status == TaskStatus.FAILED),
            "sa_structural_fix_pending": sum(
                1
                for task in tasks
                if task.status == TaskStatus.PENDING and str(task.strategy or "") == "sa_structural_fix"
            ),
            "sa_structural_fix_active": sum(
                1
                for task in tasks
                if task.status == TaskStatus.ACTIVE and str(task.strategy or "") == "sa_structural_fix"
            ),
        }

    def _latest_classification_for_task(self, task_id: str) -> str | None:
        for run in self.store.list_control_worker_runs(task_id=task_id):
            if run.classification:
                return run.classification
        return None

    def _default_corrective_objective(self, task: Task, classification: str | None) -> str:
        classification_text = classification or "structural failure"
        return (
            "This is an sa-watch structural corrective task.\n"
            f"The task '{task.title}' is recurring because of {classification_text}.\n"
            "Do not produce another report-only attempt.\n"
            "Make a real architectural or workflow change in the repository that prevents this failure mode from recurring.\n"
            "Required proof:\n"
            "- explain the root cause precisely\n"
            "- implement the preventative change\n"
            "- add or update tests that reproduce the prior failure mode and prove it no longer recurs\n"
            "- leave durable evidence artifacts for the fix\n"
        )

    def _default_objective_corrective_objective(self, objective, classification: str | None) -> str:
        classification_text = classification or "stalled objective workflow"
        return (
            "This is an sa-watch structural corrective task.\n"
            f"The objective '{objective.title}' is not making forward progress because of {classification_text}.\n"
            "Do not merely restart the stalled workflow.\n"
            "Make a real architectural or workflow change that prevents this kind of objective stall from recurring.\n"
            "Required proof:\n"
            "- explain why the workflow stopped advancing\n"
            "- implement the preventative change\n"
            "- add or update tests that reproduce the stall and prove the objective advances afterward\n"
            "- leave durable evidence artifacts for the fix\n"
        )

    def _matching_corrective_tasks(self, task_id: str, classification: str | None) -> list[Task]:
        base_ref = f"{task_id}:{classification or 'structural'}"
        matches: list[Task] = []
        for task in self.store.list_tasks():
            if task.external_ref_type != "sa_watch":
                continue
            ref_id = str(task.external_ref_id or "")
            if ref_id == base_ref or ref_id.startswith(f"{base_ref}:retry:"):
                matches.append(task)
        return matches

    def _next_corrective_ref_id(self, task_id: str, classification: str | None) -> str:
        base_ref = f"{task_id}:{classification or 'structural'}"
        matches = self._matching_corrective_tasks(task_id, classification)
        if not matches:
            return base_ref
        return f"{base_ref}:retry:{len(matches) + 1}"

    def _matching_objective_corrective_tasks(self, objective_id: str, classification: str | None) -> list[Task]:
        base_ref = f"objective:{objective_id}:{classification or 'structural'}"
        matches: list[Task] = []
        for task in self.store.list_tasks():
            if task.external_ref_type != "sa_watch":
                continue
            ref_id = str(task.external_ref_id or "")
            if ref_id == base_ref or ref_id.startswith(f"{base_ref}:retry:"):
                matches.append(task)
        return matches

    def _next_objective_corrective_ref_id(self, objective_id: str, classification: str | None) -> str:
        base_ref = f"objective:{objective_id}:{classification or 'structural'}"
        matches = self._matching_objective_corrective_tasks(objective_id, classification)
        if not matches:
            return base_ref
        return f"{base_ref}:retry:{len(matches) + 1}"

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
                "last_activity_at": latest_atomic.get("last_activity_at").isoformat(),
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
        phase = ""
        if progress:
            phase = str(progress[-1].metadata.get("phase") or "")
        return {
            "generation_id": generation_id,
            "status": status,
            "phase": phase,
            "last_activity_at": last_activity,
            "is_stale": status == "running" and (datetime.now(UTC) - last_activity) > timedelta(minutes=5),
        }
