from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from .domain import (
    ControlEvent,
    ControlLaneState,
    ControlLaneStateValue,
    ControlRecoveryAction,
    ControlSystemState,
    GlobalSystemState,
    new_id,
)
from .store import SQLiteHarnessStore


DEFAULT_CONTROL_LANES = ("api", "harness", "worker", "watch", "telegram")


class ControlPlane:
    def __init__(self, store: SQLiteHarnessStore, *, producer: str = "control-plane") -> None:
        self.store = store
        self.producer = producer
        self.store.ensure_control_lanes(list(DEFAULT_CONTROL_LANES))

    def turn_on(self) -> dict[str, object]:
        state = replace(
            self.store.get_control_system_state(),
            global_state=GlobalSystemState.STARTING,
            master_switch=True,
            freeze_reason=None,
            updated_at=datetime.now(UTC),
        )
        self.store.update_control_system_state(state)
        for lane in self.store.list_control_lane_states():
            if lane.state == ControlLaneStateValue.DISABLED:
                continue
            self.store.update_control_lane_state(
                replace(lane, state=ControlLaneStateValue.RUNNING, reason="system_on", cooldown_until=None, updated_at=datetime.now(UTC))
            )
        self._record_event("system_on", "system", "system", {"global_state": state.global_state.value})
        return self.status()

    def turn_off(self) -> dict[str, object]:
        state = replace(
            self.store.get_control_system_state(),
            global_state=GlobalSystemState.OFF,
            master_switch=False,
            freeze_reason=None,
            updated_at=datetime.now(UTC),
        )
        self.store.update_control_system_state(state)
        for lane in self.store.list_control_lane_states():
            if lane.state == ControlLaneStateValue.DISABLED:
                continue
            self.store.update_control_lane_state(
                replace(lane, state=ControlLaneStateValue.PAUSED, reason="system_off", cooldown_until=None, updated_at=datetime.now(UTC))
            )
        self._record_event("system_off", "system", "system", {"global_state": state.global_state.value})
        return self.status()

    def freeze(self, reason: str) -> dict[str, object]:
        state = replace(
            self.store.get_control_system_state(),
            global_state=GlobalSystemState.FROZEN,
            freeze_reason=reason,
            updated_at=datetime.now(UTC),
        )
        self.store.update_control_system_state(state)
        for lane in self.store.list_control_lane_states():
            if lane.state == ControlLaneStateValue.DISABLED:
                continue
            self.store.update_control_lane_state(
                replace(lane, state=ControlLaneStateValue.PAUSED, reason="frozen", cooldown_until=None, updated_at=datetime.now(UTC))
            )
        self.store.create_control_recovery_action(
            ControlRecoveryAction(
                id=new_id("recovery"),
                action_type="freeze",
                target_type="system",
                target_id="system",
                reason=reason,
                result="applied",
            )
        )
        self._record_event("system_frozen", "system", "system", {"reason": reason})
        return self.status()

    def thaw(self) -> dict[str, object]:
        current = self.store.get_control_system_state()
        next_state = GlobalSystemState.STARTING if current.master_switch else GlobalSystemState.OFF
        state = replace(
            current,
            global_state=next_state,
            freeze_reason=None,
            updated_at=datetime.now(UTC),
        )
        self.store.update_control_system_state(state)
        if current.master_switch:
            for lane in self.store.list_control_lane_states():
                if lane.state == ControlLaneStateValue.DISABLED:
                    continue
                self.store.update_control_lane_state(
                    replace(lane, state=ControlLaneStateValue.RUNNING, reason="thawed", cooldown_until=None, updated_at=datetime.now(UTC))
                )
        self._record_event("system_thawed", "system", "system", {"global_state": state.global_state.value})
        return self.status()

    def pause_lane(self, lane_name: str, *, reason: str = "operator_pause") -> dict[str, object]:
        lane = self._require_lane(lane_name)
        self.store.update_control_lane_state(
            replace(lane, state=ControlLaneStateValue.PAUSED, reason=reason, cooldown_until=None, updated_at=datetime.now(UTC))
        )
        self._record_event("lane_paused", "lane", lane_name, {"reason": reason})
        return self.status()

    def resume_lane(self, lane_name: str, *, reason: str = "operator_resume") -> dict[str, object]:
        if self.store.get_control_system_state().global_state == GlobalSystemState.FROZEN:
            raise ValueError("Cannot resume a lane while the system is frozen")
        lane = self._require_lane(lane_name)
        next_state = ControlLaneStateValue.RUNNING if self.store.get_control_system_state().master_switch else ControlLaneStateValue.PAUSED
        self.store.update_control_lane_state(
            replace(lane, state=next_state, reason=reason, cooldown_until=None, updated_at=datetime.now(UTC))
        )
        self._record_event("lane_resumed", "lane", lane_name, {"reason": reason, "state": next_state.value})
        return self.status()

    def status(self) -> dict[str, object]:
        system = self.store.get_control_system_state()
        lanes = {lane.lane_name: lane.state.value for lane in self.store.list_control_lane_states()}
        cooldowns = [
            {
                "lane": lane.lane_name,
                "until": lane.cooldown_until.isoformat(),
                "reason": lane.reason,
            }
            for lane in self.store.list_control_lane_states()
            if lane.state == ControlLaneStateValue.COOLDOWN and lane.cooldown_until is not None
        ]
        latest_merge_event = next(
            iter(self.store.list_control_events(limit=1, event_type="merge_succeeded") or self.store.list_control_events(limit=1, event_type="merge_failed")),
            None,
        )
        failure_event = next(iter(self.store.list_control_events(limit=1, event_type="provider_degraded")), None)
        return {
            "global_state": system.global_state.value,
            "master_switch": system.master_switch,
            "lanes": lanes,
            "active_task_id": None,
            "latest_failure_class": failure_event.payload.get("class") if failure_event else None,
            "cooldowns": cooldowns,
            "last_merge_status": latest_merge_event.event_type.removeprefix("merge_") if latest_merge_event else None,
            "frozen_reason": system.freeze_reason,
        }

    def _require_lane(self, lane_name: str) -> ControlLaneState:
        lane = self.store.get_control_lane_state(lane_name)
        if lane is None:
            raise ValueError(f"Unknown control lane: {lane_name}")
        return lane

    def _record_event(self, event_type: str, entity_type: str, entity_id: str, payload: dict[str, object]) -> None:
        now = datetime.now(UTC)
        self.store.create_control_event(
            ControlEvent(
                id=new_id("control_event"),
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                producer=self.producer,
                payload=payload,
                idempotency_key=f"{event_type}:{entity_type}:{entity_id}:{int(now.timestamp() * 1000000)}",
                created_at=now,
            )
        )
