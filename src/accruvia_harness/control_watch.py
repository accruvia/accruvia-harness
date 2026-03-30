from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.request import urlopen

from .control_breadcrumbs import BreadcrumbWriter
from .control_classifier import FailureClassifier
from .control_plane import ControlPlane
from .domain import ControlLaneStateValue, GlobalSystemState, ObjectiveStatus, new_id
from .store import SQLiteHarnessStore


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
        results: dict[str, object] = {}
        system = self.store.get_control_system_state()
        healthy = True
        reasons: list[str] = []

        if api_url:
            api_result = self.check_api(api_url)
            results["api"] = api_result
            healthy = healthy and bool(api_result["ok"])
            if not api_result["ok"]:
                reasons.append("api_down")

        harness_result = self.check_harness()
        results["harness"] = harness_result
        healthy = healthy and bool(harness_result["ok"])
        if not harness_result["ok"]:
            reasons.append("harness_down")

        stalled = self.find_stalled_objectives(hours=stalled_objective_hours)
        results["stalled_objectives"] = stalled
        if stalled:
            healthy = False
            reasons.append("objective_stalled")
            if freeze_on_stall:
                self.control_plane.freeze(
                    "objective_stalled:" + ",".join(item["objective_id"] for item in stalled[:3])
                )

        if system.master_switch and system.global_state != GlobalSystemState.FROZEN:
            if healthy:
                self.control_plane.mark_healthy()
            else:
                self.control_plane.mark_degraded(",".join(reasons))
        results["status"] = self.control_plane.status()
        return results

    def check_api(self, api_url: str) -> dict[str, object]:
        lane = self.store.get_control_lane_state("api")
        assert lane is not None
        try:
            body = self._fetch_api_body(api_url)
            self.store.update_control_lane_state(
                replace(lane, state=ControlLaneStateValue.RUNNING, reason="api_up", cooldown_until=None, updated_at=datetime.now(UTC))
            )
            self.store.create_control_event(
                self._control_event("api_up", "lane", "api", {"url": api_url, "body_preview": body[:200]})
            )
            return {"ok": True, "url": api_url, "body_preview": body[:200]}
        except Exception as exc:
            classification = self.classifier.classify(str(exc))
            if "connection refused" in str(exc).lower():
                classification = replace(classification, classification="system_failure", retry_recommended=True, cooldown_seconds=0)
            restarted = False
            if self.restart_api is not None and self._restart_allowed("api", classification.classification):
                restarted = bool(self.restart_api())
                if restarted:
                    body = self._await_api_recovery(api_url)
                    if body is not None:
                        self._record_restart("api", classification.classification, "restarted")
                        self.store.update_control_lane_state(
                            replace(lane, state=ControlLaneStateValue.RUNNING, reason="api_restarted", cooldown_until=None, updated_at=datetime.now(UTC))
                        )
                        self.store.create_control_event(
                            self._control_event("api_up", "lane", "api", {"url": api_url, "body_preview": body[:200], "restarted": True})
                        )
                        return {"ok": True, "url": api_url, "body_preview": body[:200], "restarted": True}
                    self._record_restart("api", classification.classification, "restart_failed")
            if classification.classification in {"provider_rate_limit", "provider_outage"} and classification.cooldown_seconds > 0:
                self.control_plane.enter_cooldown("api", reason=classification.classification, seconds=classification.cooldown_seconds)
            else:
                self.store.update_control_lane_state(
                    replace(
                        lane,
                        state=ControlLaneStateValue.PAUSED,
                        reason=classification.classification,
                        cooldown_until=None,
                        updated_at=datetime.now(UTC),
                    )
                )
                self.store.create_control_event(
                    self._control_event(
                        "api_down",
                        "lane",
                        "api",
                        {"url": api_url, "class": classification.classification, "message": str(exc)},
                    )
                )
            self.breadcrumb_writer.write_bundle(
                entity_type="lane",
                entity_id="api",
                meta={"lane": "api", "url": api_url},
                evidence={"error": str(exc)},
                decision={
                    "classification": classification.classification,
                    "retry_recommended": classification.retry_recommended,
                    "cooldown_seconds": classification.cooldown_seconds,
                },
                classification=classification.classification,
                summary=f"API check failed for {api_url}: {classification.classification}",
            )
            return {"ok": False, "url": api_url, "classification": classification.classification, "message": str(exc)}

    def check_harness(self) -> dict[str, object]:
        lane = self.store.get_control_lane_state("harness")
        assert lane is not None
        running = self._running_supervisors()
        if running:
            self.store.update_control_lane_state(
                replace(lane, state=ControlLaneStateValue.RUNNING, reason="supervisor_running", cooldown_until=None, updated_at=datetime.now(UTC))
            )
            self.store.create_control_event(
                self._control_event("harness_up", "lane", "harness", {"supervisor_count": len(running)})
            )
            return {"ok": True, "supervisor_count": len(running), "supervisors": running}

        if self.restart_harness is not None and self._restart_allowed("harness", "no_supervisor"):
            restarted = self.restart_harness()
            if restarted:
                running = self._await_supervisor_recovery()
                self._record_restart("harness", "no_supervisor", "restarted" if running else "restart_failed")
                if running:
                    self.store.update_control_lane_state(
                        replace(lane, state=ControlLaneStateValue.RUNNING, reason="supervisor_restarted", cooldown_until=None, updated_at=datetime.now(UTC))
                    )
                    self.store.create_control_event(
                        self._control_event("harness_up", "lane", "harness", {"supervisor_count": len(running), "restarted": True})
                    )
                    return {"ok": True, "supervisor_count": len(running), "supervisors": running, "restarted": True}

        self.store.update_control_lane_state(
            replace(lane, state=ControlLaneStateValue.PAUSED, reason="no_supervisor", cooldown_until=None, updated_at=datetime.now(UTC))
        )
        self.store.create_control_event(
            self._control_event("harness_down", "lane", "harness", {"supervisor_count": 0})
        )
        self.breadcrumb_writer.write_bundle(
            entity_type="lane",
            entity_id="harness",
            meta={"lane": "harness"},
            evidence={"supervisors": []},
            decision={"classification": "system_failure", "retry_recommended": True, "cooldown_seconds": 0},
            classification="system_failure",
            summary="Harness watch detected no running supervisor processes.",
        )
        return {"ok": False, "supervisor_count": 0, "supervisors": []}

    def _fetch_api_body(self, api_url: str) -> str:
        with urlopen(api_url, timeout=5) as response:
            return response.read(2000).decode("utf-8", errors="replace")

    def _await_api_recovery(self, api_url: str, *, timeout_seconds: float = 10.0) -> str | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                return self._fetch_api_body(api_url)
            except Exception:
                time.sleep(0.25)
        return None

    def _await_supervisor_recovery(self, *, timeout_seconds: float = 10.0) -> list[dict[str, object]]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            running = self._running_supervisors()
            if running:
                return running
            time.sleep(0.25)
        return []

    def _restart_allowed(self, target_id: str, reason: str) -> bool:
        recent = self.store.list_control_recovery_actions(target_type="lane", target_id=target_id)
        now = datetime.now(UTC)
        for action in recent[:5]:
            if action.action_type != "restart":
                continue
            if action.reason != reason:
                continue
            age_seconds = (now - action.created_at).total_seconds()
            if age_seconds < 60:
                return False
        return True

    def _record_restart(self, target_id: str, reason: str, result: str) -> None:
        self.store.create_control_recovery_action(
            self._recovery_action("restart", "lane", target_id, reason, result)
        )

    def find_stalled_objectives(self, *, hours: float = 6.0) -> list[dict[str, object]]:
        stalled: list[dict[str, object]] = []
        cutoff_seconds = hours * 3600.0
        now = datetime.now(UTC)
        for objective in self.store.list_objectives():
            if objective.status == ObjectiveStatus.RESOLVED:
                continue
            age_seconds = (now - objective.updated_at).total_seconds()
            linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
            rejected_promotions = 0
            for task in linked_tasks:
                promotions = self.store.list_promotions(task.id)
                rejected_promotions += sum(1 for promotion in promotions[-2:] if promotion.status.value == "rejected")
            if age_seconds >= cutoff_seconds or rejected_promotions >= 2:
                payload = {
                    "objective_id": objective.id,
                    "hours_without_progress": round(age_seconds / 3600.0, 2),
                    "failed_promotion_cycles": rejected_promotions,
                }
                self.store.create_control_event(
                    self._control_event("objective_stalled", "objective", objective.id, payload)
                )
                self.breadcrumb_writer.write_bundle(
                    entity_type="objective",
                    entity_id=objective.id,
                    meta={"objective_id": objective.id, "title": objective.title},
                    evidence={"hours_without_progress": payload["hours_without_progress"], "failed_promotion_cycles": rejected_promotions},
                    decision={"classification": "objective_stalled", "retry_recommended": False, "cooldown_seconds": 0},
                    classification="objective_stalled",
                    summary=f"Objective {objective.id} appears stalled.",
                )
                stalled.append(payload)
        return stalled

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

    def _control_event(self, event_type: str, entity_type: str, entity_id: str, payload: dict[str, object]):
        from .domain import ControlEvent

        return ControlEvent(
            id=new_id("control_event"),
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            producer="control-watch",
            payload=payload,
            idempotency_key=new_id("event_key"),
        )

    def _recovery_action(self, action_type: str, target_type: str, target_id: str, reason: str, result: str):
        from .domain import ControlRecoveryAction

        return ControlRecoveryAction(
            id=new_id("recovery"),
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
            result=result,
        )
