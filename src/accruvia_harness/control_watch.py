from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
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
    ) -> None:
        self.store = store
        self.control_plane = control_plane
        self.classifier = classifier
        self.breadcrumb_writer = breadcrumb_writer
        self.supervisor_control_dir = Path(supervisor_control_dir)

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
            with urlopen(api_url, timeout=5) as response:
                body = response.read(2000).decode("utf-8", errors="replace")
            self.store.update_control_lane_state(
                replace(lane, state=ControlLaneStateValue.RUNNING, reason="api_up", cooldown_until=None, updated_at=datetime.now(UTC))
            )
            self.store.create_control_event(
                self._control_event("api_up", "lane", "api", {"url": api_url, "body_preview": body[:200]})
            )
            return {"ok": True, "url": api_url, "body_preview": body[:200]}
        except Exception as exc:
            classification = self.classifier.classify(str(exc))
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
