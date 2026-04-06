from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from .control_breadcrumbs import BreadcrumbWriter
from .control_classifier import FailureClassifier
from .control_plane import ControlPlane
from .domain import (
    ControlLaneStateValue,
    ControlRecoveryAction,
    ControlEvent,
    PromotionStatus,
    RunStatus,
    Task,
    TaskStatus,
    new_id,
)
from .store import SQLiteHarnessStore


NO_ACTIVE_TASKS_TIMEOUT_SECONDS = 120
NO_ARTIFACT_TIMEOUT_SECONDS = 600
RECONCILE_TIMEOUT_SECONDS = 120
MISSING_PREREQUISITE_TIMEOUT_SECONDS = 120
SAME_STATE_LOOP_THRESHOLD = 2
SAME_STATE_LOOP_HARD_MAX = 3
NON_MEANINGFUL_ARTIFACT_KINDS = frozenset({"heartbeat", "stdout", "stderr"})
TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.BLOCKED, RunStatus.DISPOSED})
OBJECTIVE_STALLED_SIGNAL_WINDOW = timedelta(minutes=30)


class ControlWatchService:
    def __init__(
        self,
        store: SQLiteHarnessStore,
        control_plane: ControlPlane,
        classifier: FailureClassifier,
        breadcrumb_writer: BreadcrumbWriter,
        *,
        supervisor_control_dir: str | Path,
        restart_api: Callable[[], dict[str, object] | None] | None = None,
        restart_harness: Callable[[], dict[str, object] | None] | None = None,
        no_active_tasks_timeout_seconds: int = NO_ACTIVE_TASKS_TIMEOUT_SECONDS,
        no_artifact_timeout_seconds: int = NO_ARTIFACT_TIMEOUT_SECONDS,
        reconcile_timeout_seconds: int = RECONCILE_TIMEOUT_SECONDS,
        missing_prerequisite_timeout_seconds: int = MISSING_PREREQUISITE_TIMEOUT_SECONDS,
        same_state_loop_threshold: int = SAME_STATE_LOOP_THRESHOLD,
        same_state_loop_hard_max: int = SAME_STATE_LOOP_HARD_MAX,
    ) -> None:
        self.store = store
        self.control_plane = control_plane
        self.classifier = classifier
        self.breadcrumb_writer = breadcrumb_writer
        self.supervisor_control_dir = Path(supervisor_control_dir)
        self.restart_api = restart_api
        self.restart_harness = restart_harness
        self.interval_seconds = 60
        self._last_invoked_at = 0.0
        self.no_active_tasks_timeout_seconds = max(int(no_active_tasks_timeout_seconds), 1)
        self.no_artifact_timeout_seconds = max(int(no_artifact_timeout_seconds), 1)
        self.reconcile_timeout_seconds = max(int(reconcile_timeout_seconds), 1)
        self.missing_prerequisite_timeout_seconds = max(int(missing_prerequisite_timeout_seconds), 1)
        self.same_state_loop_threshold = max(int(same_state_loop_threshold), 1)
        self.same_state_loop_hard_max = max(int(same_state_loop_hard_max), self.same_state_loop_threshold)

    def observe(self, event: dict[str, object], *, api_url: str | None = None) -> dict[str, object] | None:
        if str(event.get("type") or "") != "sleeping":
            return None
        if time.monotonic() - self._last_invoked_at < self.interval_seconds:
            return None
        self._last_invoked_at = time.monotonic()
        return self.run_once(api_url=api_url)

    def run_once(
        self,
        *,
        api_url: str | None = None,
        stalled_objective_hours: float = 6.0,
        freeze_on_stall: bool = True,
    ) -> dict[str, object]:
        del api_url, stalled_objective_hours, freeze_on_stall
        self._check_budget_recovery()
        evaluation = self._evaluate_stuck_state()
        matched_rules = list(evaluation["matched_rules"])
        if matched_rules:
            self.control_plane.mark_degraded(",".join(matched_rules))
            self._record_stuck_event(evaluation)
            self._write_stuck_breadcrumbs(evaluation)
        else:
            self.control_plane.mark_healthy(reason="stuck_checks_passed")
        self._record_state_snapshots(evaluation)
        evaluation["status"] = self.control_plane.status()
        return evaluation

    def _check_budget_recovery(self) -> None:
        """If the worker lane is paused due to budget/credit exhaustion and the
        budget has since recovered, automatically resume the lane.

        Runs every control-loop tick (~60s). Cheap: one lane-state read + one
        cost-tracker check. No LLM calls.
        """
        lane = self.store.get_control_lane_state("worker")
        if lane is None or lane.state != ControlLaneStateValue.PAUSED:
            return
        reason = str(lane.reason or "").lower()
        if "budget" not in reason and "credit" not in reason:
            return
        # Budget was the pause reason — check if it's recovered
        try:
            from .cost_tracker import CostTracker

            tracker = CostTracker(self.store.db_path.parent)
            # Check all projects; resume if ANY is within budget
            for project in self.store.list_projects():
                within_budget, _ = tracker.check_budget(project.id)
                if within_budget:
                    self.control_plane.resume_lane(
                        "worker", reason="budget_recovered"
                    )
                    self.store.create_control_event(
                        ControlEvent(
                            id=new_id("control_event"),
                            event_type="budget_recovered",
                            entity_type="lane",
                            entity_id="worker",
                            producer="control-watch",
                            payload={"project_id": project.id},
                            idempotency_key=new_id("event_key"),
                        )
                    )
                    return
        except Exception:  # noqa: BLE001
            pass  # cost_tracker unavailable — don't block the watch loop

    def _evaluate_stuck_state(self) -> dict[str, object]:
        now = datetime.now(UTC)
        tasks = self.store.list_tasks()
        task_by_id = {task.id: task for task in tasks}
        runs_by_task = {task.id: self.store.list_runs(task.id) for task in tasks}
        promotions_by_task = {task.id: self.store.list_promotions(task.id) for task in tasks}
        leases = {lease.task_id: lease for lease in self.store.list_task_leases()}
        supervisors = self._running_supervisors()
        running_worker_ids = {
            str(payload.get("worker_id") or "").strip()
            for payload in supervisors
            if str(payload.get("worker_id") or "").strip()
        }
        objectives = {objective.id: objective for objective in self.store.list_objectives()}

        matched_rules: list[str] = []
        reasons: list[dict[str, object]] = []
        affected_task_ids: set[str] = set()
        affected_promotion_ids: set[str] = set()

        active_tasks = [task for task in tasks if task.status == TaskStatus.ACTIVE]
        pending_tasks = [task for task in tasks if task.status == TaskStatus.PENDING]
        if pending_tasks and not active_tasks:
            latest_pending_update = max(task.updated_at for task in pending_tasks)
            idle_seconds = (now - latest_pending_update).total_seconds()
            if idle_seconds >= self.no_active_tasks_timeout_seconds:
                matched_rules.append("No active tasks while work exists")
                reasons.append(
                    {
                        "rule": "No active tasks while work exists",
                        "seconds_idle": round(idle_seconds, 1),
                        "task_ids": [task.id for task in pending_tasks],
                    }
                )
                affected_task_ids.update(task.id for task in pending_tasks)

        if pending_tasks and supervisors:
            oldest_pending_age = max((now - task.updated_at).total_seconds() for task in pending_tasks)
            if oldest_pending_age >= self.no_active_tasks_timeout_seconds and not active_tasks:
                matched_rules.append("Pending work is not being claimed")
                reasons.append(
                    {
                        "rule": "Pending work is not being claimed",
                        "seconds_without_claim": round(oldest_pending_age, 1),
                        "task_ids": [task.id for task in pending_tasks],
                    }
                )
                affected_task_ids.update(task.id for task in pending_tasks)

        stalled_objective_ids: list[str] = []
        stalled_event_ids = set()
        for event in self.store.list_control_events(event_type="objective_stalled", limit=20):
            stalled_event_ids.add(event.entity_id)
            objective = objectives.get(event.entity_id)
            if objective is None:
                continue
            if objective.status.value == "resolved":
                continue
            if event.created_at < now - OBJECTIVE_STALLED_SIGNAL_WINDOW:
                continue
            stalled_objective_ids.append(objective.id)
        for objective in objectives.values():
            if objective.id in stalled_objective_ids:
                continue
            if objective.status.value == "paused":
                stalled_objective_ids.append(objective.id)
        if stalled_objective_ids:
            matched_rules.append("Stalled objective exists")
            reasons.append(
                {
                    "rule": "Stalled objective exists",
                    "objective_ids": sorted(dict.fromkeys(stalled_objective_ids)),
                }
            )

        for task in active_tasks:
            latest_run = runs_by_task.get(task.id, [])[-1] if runs_by_task.get(task.id) else None
            run_dir = self._run_dir(latest_run.id) if latest_run is not None else None
            artifact_info = self._artifact_inventory(run_dir) if run_dir is not None else self._empty_artifact_inventory()

            if artifact_info["latest_artifact_age_seconds"] is not None and artifact_info["latest_artifact_age_seconds"] >= self.no_artifact_timeout_seconds:
                matched_rules.append("Active task produced no artifact")
                reasons.append(
                    {
                        "rule": "Active task produced no artifact",
                        "task_id": task.id,
                        "run_id": latest_run.id if latest_run is not None else None,
                        "seconds_since_artifact": round(float(artifact_info["latest_artifact_age_seconds"]), 1),
                    }
                )
                affected_task_ids.add(task.id)
            elif artifact_info["latest_artifact_age_seconds"] is None and (now - task.updated_at).total_seconds() >= self.no_artifact_timeout_seconds:
                matched_rules.append("Active task produced no artifact")
                reasons.append(
                    {
                        "rule": "Active task produced no artifact",
                        "task_id": task.id,
                        "run_id": latest_run.id if latest_run is not None else None,
                        "seconds_since_activity": round((now - task.updated_at).total_seconds(), 1),
                    }
                )
                affected_task_ids.add(task.id)

            if (
                artifact_info["recent_artifact_count"] > 0
                and artifact_info["recent_meaningful_artifact_count"] == 0
                and artifact_info["recent_window_age_seconds"] >= self.no_artifact_timeout_seconds
            ):
                matched_rules.append("Active task produced only liveness noise")
                reasons.append(
                    {
                        "rule": "Active task produced only liveness noise",
                        "task_id": task.id,
                        "run_id": latest_run.id if latest_run is not None else None,
                        "artifact_kinds": sorted(set(artifact_info["recent_artifact_kinds"])),
                    }
                )
                affected_task_ids.add(task.id)
            elif (
                artifact_info["latest_artifact_kind"] in NON_MEANINGFUL_ARTIFACT_KINDS
                and artifact_info["latest_artifact_age_seconds"] is not None
                and artifact_info["latest_artifact_age_seconds"] >= self.no_artifact_timeout_seconds
            ):
                matched_rules.append("Active task produced only liveness noise")
                reasons.append(
                    {
                        "rule": "Active task produced only liveness noise",
                        "task_id": task.id,
                        "run_id": latest_run.id if latest_run is not None else None,
                        "artifact_kinds": [artifact_info["latest_artifact_kind"]],
                    }
                )
                affected_task_ids.add(task.id)

            lease = leases.get(task.id)
            lease_worker_id = str(lease.worker_id).strip() if lease is not None else ""
            if lease is None or (running_worker_ids and lease_worker_id and lease_worker_id not in running_worker_ids) or (not supervisors):
                matched_rules.append("Active task lost its worker")
                reasons.append(
                    {
                        "rule": "Active task lost its worker",
                        "task_id": task.id,
                        "lease_present": lease is not None,
                        "lease_worker_id": lease_worker_id,
                        "running_supervisor_count": len(supervisors),
                    }
                )
                affected_task_ids.add(task.id)

            if latest_run is not None and latest_run.status in TERMINAL_RUN_STATUSES:
                reconcile_age = (now - latest_run.updated_at).total_seconds()
                if reconcile_age >= self.reconcile_timeout_seconds:
                    matched_rules.append("Run finished but state did not reconcile")
                    reasons.append(
                        {
                            "rule": "Run finished but state did not reconcile",
                            "task_id": task.id,
                            "run_id": latest_run.id,
                            "run_status": latest_run.status.value,
                            "seconds_since_run_end": round(reconcile_age, 1),
                        }
                    )
                    affected_task_ids.add(task.id)

        for task in tasks:
            if task.status not in {TaskStatus.PENDING, TaskStatus.ACTIVE}:
                continue
            latest_run = runs_by_task.get(task.id, [])[-1] if runs_by_task.get(task.id) else None
            run_dir = self._run_dir(latest_run.id) if latest_run is not None else None
            artifact_info = self._artifact_inventory(run_dir) if run_dir is not None else self._empty_artifact_inventory()
            task_loop = self._same_state_loop(
                entity_type="task",
                entity_id=task.id,
                fingerprint=self._task_state_fingerprint(task, latest_run, artifact_info, task.id in leases),
            )
            if task_loop >= self.same_state_loop_threshold:
                matched_rules.append("Task is looping in the same state")
                reasons.append(
                    {
                        "rule": "Task is looping in the same state",
                        "task_id": task.id,
                        "consecutive_loops": task_loop,
                        "hard_max_reached": task_loop >= self.same_state_loop_hard_max,
                    }
                )
                affected_task_ids.add(task.id)

        for task in tasks:
            promotions = promotions_by_task.get(task.id, [])
            if not promotions:
                continue
            promotion = promotions[-1]
            if promotion.status != PromotionStatus.PENDING:
                continue
            promotion_age = (now - promotion.created_at).total_seconds()
            artifacts = self.store.list_artifacts(promotion.run_id)
            artifact_kinds = {artifact.kind for artifact in artifacts}
            latest_artifact_at = max((artifact.created_at for artifact in artifacts), default=None)
            if latest_artifact_at is None:
                latest_movement_age = promotion_age
            else:
                latest_movement_age = (now - latest_artifact_at).total_seconds()

            if latest_movement_age >= self.no_artifact_timeout_seconds:
                matched_rules.append("Promotion produced no movement")
                reasons.append(
                    {
                        "rule": "Promotion produced no movement",
                        "promotion_id": promotion.id,
                        "task_id": task.id,
                        "seconds_without_movement": round(latest_movement_age, 1),
                    }
                )
                affected_task_ids.add(task.id)
                affected_promotion_ids.add(promotion.id)

            missing_required = sorted(kind for kind in task.required_artifacts if kind not in artifact_kinds)
            if missing_required and promotion_age >= self.missing_prerequisite_timeout_seconds:
                matched_rules.append("Promotion is blocked on a missing prerequisite")
                reasons.append(
                    {
                        "rule": "Promotion is blocked on a missing prerequisite",
                        "promotion_id": promotion.id,
                        "task_id": task.id,
                        "missing_required_artifacts": missing_required,
                        "seconds_blocked": round(promotion_age, 1),
                    }
                )
                affected_task_ids.add(task.id)
                affected_promotion_ids.add(promotion.id)

            promotion_loop = self._same_state_loop(
                entity_type="promotion",
                entity_id=promotion.id,
                fingerprint=self._promotion_state_fingerprint(promotion, task, artifact_kinds),
            )
            if promotion_loop >= self.same_state_loop_threshold:
                matched_rules.append("Promotion is looping in the same state")
                reasons.append(
                    {
                        "rule": "Promotion is looping in the same state",
                        "promotion_id": promotion.id,
                        "task_id": task.id,
                        "consecutive_loops": promotion_loop,
                        "hard_max_reached": promotion_loop >= self.same_state_loop_hard_max,
                    }
                )
                affected_task_ids.add(task.id)
                affected_promotion_ids.add(promotion.id)

        deduped_rules = list(dict.fromkeys(matched_rules))
        return {
            "stuck": bool(deduped_rules),
            "matched_rules": deduped_rules,
            "reasons": reasons,
            "affected_task_ids": sorted(affected_task_ids),
            "affected_promotion_ids": sorted(affected_promotion_ids),
            "supervisor_count": len(supervisors),
        }

    def _record_stuck_event(self, evaluation: dict[str, object]) -> None:
        payload = {
            "matched_rules": list(evaluation["matched_rules"]),
            "reasons": list(evaluation["reasons"]),
            "affected_task_ids": list(evaluation["affected_task_ids"]),
            "affected_promotion_ids": list(evaluation["affected_promotion_ids"]),
        }
        self.store.create_control_event(
            ControlEvent(
                id=new_id("control_event"),
                event_type="stuck_detected",
                entity_type="system",
                entity_id="system",
                producer="control-watch",
                payload=payload,
                idempotency_key=new_id("event_key"),
            )
        )
        self.store.create_control_recovery_action(
            ControlRecoveryAction(
                id=new_id("recovery"),
                action_type="observe",
                target_type="system",
                target_id="system",
                reason="stuck_detected",
                result="recorded",
            )
        )

    def _write_stuck_breadcrumbs(self, evaluation: dict[str, object]) -> None:
        for task_id in evaluation["affected_task_ids"]:
            matching_reasons = [item for item in evaluation["reasons"] if item.get("task_id") == task_id]
            summary = ", ".join(str(item.get("rule") or "") for item in matching_reasons[:3]) or "Task appears stuck."
            self.breadcrumb_writer.write_bundle(
                entity_type="task",
                entity_id=task_id,
                meta={"task_id": task_id},
                evidence={"reasons": matching_reasons},
                decision={"matched_rules": evaluation["matched_rules"], "stuck": True},
                classification="stuck_detected",
                summary=summary,
            )
        for promotion_id in evaluation["affected_promotion_ids"]:
            matching_reasons = [item for item in evaluation["reasons"] if item.get("promotion_id") == promotion_id]
            summary = ", ".join(str(item.get("rule") or "") for item in matching_reasons[:3]) or "Promotion appears stuck."
            self.breadcrumb_writer.write_bundle(
                entity_type="promotion",
                entity_id=promotion_id,
                meta={"promotion_id": promotion_id},
                evidence={"reasons": matching_reasons},
                decision={"matched_rules": evaluation["matched_rules"], "stuck": True},
                classification="stuck_detected",
                summary=summary,
            )

    def _record_state_snapshots(self, evaluation: dict[str, object]) -> None:
        now = datetime.now(UTC)
        tasks = self.store.list_tasks()
        leases = {lease.task_id: lease for lease in self.store.list_task_leases()}
        for task in tasks:
            latest_run = self.store.list_runs(task.id)[-1] if self.store.list_runs(task.id) else None
            run_dir = self._run_dir(latest_run.id) if latest_run is not None else None
            artifact_info = self._artifact_inventory(run_dir) if run_dir is not None else self._empty_artifact_inventory()
            fingerprint = self._task_state_fingerprint(task, latest_run, artifact_info, task.id in leases)
            self.store.create_control_event(
                ControlEvent(
                    id=new_id("control_event"),
                    event_type="stuck_snapshot",
                    entity_type="task",
                    entity_id=task.id,
                    producer="control-watch",
                    payload={"fingerprint": fingerprint, "stuck": bool(evaluation["stuck"])},
                    idempotency_key=new_id("event_key"),
                    created_at=now,
                )
            )
            promotions = self.store.list_promotions(task.id)
            if not promotions:
                continue
            promotion = promotions[-1]
            artifact_kinds = {artifact.kind for artifact in self.store.list_artifacts(promotion.run_id)}
            self.store.create_control_event(
                ControlEvent(
                    id=new_id("control_event"),
                    event_type="stuck_snapshot",
                    entity_type="promotion",
                    entity_id=promotion.id,
                    producer="control-watch",
                    payload={
                        "fingerprint": self._promotion_state_fingerprint(promotion, task, artifact_kinds),
                        "stuck": bool(evaluation["stuck"]),
                    },
                    idempotency_key=new_id("event_key"),
                    created_at=now,
                )
            )

    def _same_state_loop(self, *, entity_type: str, entity_id: str, fingerprint: str) -> int:
        events = self.store.list_control_events(
            event_type="stuck_snapshot",
            entity_type=entity_type,
            entity_id=entity_id,
            limit=self.same_state_loop_hard_max - 1,
        )
        count = 1
        for event in events:
            payload = dict(event.payload or {})
            if str(payload.get("fingerprint") or "") != fingerprint:
                break
            count += 1
        return count

    def _task_state_fingerprint(self, task: Task, latest_run, artifact_info: dict[str, object], lease_present: bool) -> str:
        return "|".join(
            [
                task.status.value,
                latest_run.status.value if latest_run is not None else "no_run",
                str(artifact_info.get("latest_artifact_kind") or "none"),
                "leased" if lease_present else "unleased",
            ]
        )

    def _promotion_state_fingerprint(self, promotion, task: Task, artifact_kinds: set[str]) -> str:
        missing_required = sorted(kind for kind in task.required_artifacts if kind not in artifact_kinds)
        return "|".join(
            [
                promotion.status.value,
                ",".join(missing_required) or "all_required_present",
                ",".join(sorted(artifact_kinds)) or "no_artifacts",
            ]
        )

    def _run_dir(self, run_id: str) -> Path:
        return (self.breadcrumb_writer.workspace_root / "runs" / run_id).resolve()

    def _artifact_inventory(self, run_dir: Path) -> dict[str, object]:
        if not run_dir.exists() or not run_dir.is_dir():
            return self._empty_artifact_inventory()
        latest_path: Path | None = None
        latest_mtime = 0.0
        recent_kinds: list[str] = []
        recent_meaningful_count = 0
        recent_window_age_seconds = 0.0
        now_epoch = time.time()
        threshold_epoch = now_epoch - self.no_artifact_timeout_seconds
        for child in run_dir.iterdir():
            if not child.is_file():
                continue
            stat = child.stat()
            kind = self._artifact_kind(child)
            if stat.st_mtime > latest_mtime:
                latest_mtime = stat.st_mtime
                latest_path = child
            if stat.st_mtime >= threshold_epoch:
                recent_kinds.append(kind)
                recent_window_age_seconds = max(recent_window_age_seconds, now_epoch - stat.st_mtime)
                if kind not in NON_MEANINGFUL_ARTIFACT_KINDS:
                    recent_meaningful_count += 1
        latest_artifact_age_seconds = max(0.0, now_epoch - latest_mtime) if latest_path is not None else None
        return {
            "latest_artifact": latest_path.name if latest_path is not None else None,
            "latest_artifact_kind": self._artifact_kind(latest_path) if latest_path is not None else None,
            "latest_artifact_age_seconds": latest_artifact_age_seconds,
            "recent_artifact_count": len(recent_kinds),
            "recent_artifact_kinds": recent_kinds,
            "recent_meaningful_artifact_count": recent_meaningful_count,
            "recent_window_age_seconds": recent_window_age_seconds,
        }

    def _empty_artifact_inventory(self) -> dict[str, object]:
        return {
            "latest_artifact": None,
            "latest_artifact_kind": None,
            "latest_artifact_age_seconds": None,
            "recent_artifact_count": 0,
            "recent_artifact_kinds": [],
            "recent_meaningful_artifact_count": 0,
            "recent_window_age_seconds": 0.0,
        }

    def _artifact_kind(self, path: Path) -> str:
        name = path.name
        if name == "worker.heartbeat.json":
            return "heartbeat"
        if name == "phase.txt":
            return "phase"
        if name == "plan.txt":
            return "plan"
        if name == "report.json":
            return "report"
        if name == "compile_output.txt":
            return "compile-output"
        if name == "test_output.txt":
            return "test-output"
        if name.endswith(".stdout.txt"):
            return "stdout"
        if name.endswith(".stderr.txt"):
            return "stderr"
        return "artifact"

    def _running_supervisors(self) -> list[dict[str, object]]:
        if not self.supervisor_control_dir.exists():
            return []
        running: list[dict[str, object]] = []
        for path in sorted(self.supervisor_control_dir.glob("*.json")):
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
            running.append(payload)
        return running
