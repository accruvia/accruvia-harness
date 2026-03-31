from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .domain import (
    ControlBudget,
    ControlCooldown,
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
EXPENSIVE_CODING_RUN_LIMIT = 3
EXPENSIVE_CODING_RUN_WINDOW = timedelta(hours=1)


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
        return self._sync_global_state(reason="system_on")

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

    def mark_degraded(self, reason: str) -> dict[str, object]:
        current = self.store.get_control_system_state()
        if current.global_state == GlobalSystemState.FROZEN:
            return self.status()
        state = replace(
            current,
            global_state=GlobalSystemState.DEGRADED,
            freeze_reason=reason,
            updated_at=datetime.now(UTC),
        )
        self.store.update_control_system_state(state)
        self._record_event("system_degraded", "system", "system", {"reason": reason})
        return self._sync_global_state(reason=reason)

    def mark_healthy(self, *, reason: str = "checks_passed") -> dict[str, object]:
        current = self.store.get_control_system_state()
        if current.global_state == GlobalSystemState.FROZEN or not current.master_switch:
            return self.status()
        self._record_event("system_healthy", "system", "system", {"reason": reason})
        return self._sync_global_state(reason=reason)

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
        return self._sync_global_state(reason="thawed")

    def pause_lane(self, lane_name: str, *, reason: str = "operator_pause") -> dict[str, object]:
        lane = self._require_lane(lane_name)
        self.store.update_control_lane_state(
            replace(lane, state=ControlLaneStateValue.PAUSED, reason=reason, cooldown_until=None, updated_at=datetime.now(UTC))
        )
        self._record_event("lane_paused", "lane", lane_name, {"reason": reason})
        return self._sync_global_state(reason=reason)

    def resume_lane(self, lane_name: str, *, reason: str = "operator_resume") -> dict[str, object]:
        if self.store.get_control_system_state().global_state == GlobalSystemState.FROZEN:
            raise ValueError("Cannot resume a lane while the system is frozen")
        lane = self._require_lane(lane_name)
        next_state = ControlLaneStateValue.RUNNING if self.store.get_control_system_state().master_switch else ControlLaneStateValue.PAUSED
        self.store.update_control_lane_state(
            replace(lane, state=next_state, reason=reason, cooldown_until=None, updated_at=datetime.now(UTC))
        )
        self._record_event("lane_resumed", "lane", lane_name, {"reason": reason, "state": next_state.value})
        return self._sync_global_state(reason=reason)

    def enter_cooldown(self, lane_name: str, *, reason: str, seconds: int) -> dict[str, object]:
        lane = self._require_lane(lane_name)
        until_at = datetime.now(UTC) + timedelta(seconds=max(seconds, 0))
        self.store.update_control_lane_state(
            replace(
                lane,
                state=ControlLaneStateValue.COOLDOWN,
                reason=reason,
                cooldown_until=until_at,
                updated_at=datetime.now(UTC),
            )
        )
        self.store.create_control_cooldown(
            ControlCooldown(
                id=new_id("cooldown"),
                scope_type="lane",
                scope_id=lane_name,
                reason=reason,
                until_at=until_at,
            )
        )
        self._record_event("provider_degraded", "lane", lane_name, {"class": reason, "cooldown_seconds": seconds})
        return self._sync_global_state(reason=reason)

    def record_budget_usage(
        self,
        *,
        budget_scope: str,
        budget_key: str,
        usage_count: int = 1,
        usage_cost_usd: float = 0.0,
        window: timedelta = EXPENSIVE_CODING_RUN_WINDOW,
    ) -> ControlBudget:
        now = datetime.now(UTC)
        window_start = now - window
        window_end = now
        existing = self.store.get_control_budget(budget_scope, budget_key, window_start, window_end)
        if existing is None:
            budget = ControlBudget(
                id=new_id("budget"),
                budget_scope=budget_scope,
                budget_key=budget_key,
                window_start=window_start,
                window_end=window_end,
                usage_count=usage_count,
                usage_cost_usd=usage_cost_usd,
                updated_at=now,
            )
        else:
            budget = replace(
                existing,
                usage_count=existing.usage_count + usage_count,
                usage_cost_usd=existing.usage_cost_usd + usage_cost_usd,
                updated_at=now,
                window_end=now,
            )
        self.store.upsert_control_budget(budget)
        return budget

    def expensive_coding_budget_exhausted(
        self,
        *,
        budget_scope: str = "worker",
        budget_key: str = "expensive_coding_runs",
    ) -> bool:
        now = datetime.now(UTC)
        budgets = self.store.list_control_budgets(budget_scope=budget_scope, budget_key=budget_key)
        total = 0
        cutoff = now - EXPENSIVE_CODING_RUN_WINDOW
        for budget in budgets:
            if budget.window_end >= cutoff:
                total += budget.usage_count
        return total > EXPENSIVE_CODING_RUN_LIMIT

    def objective_no_progress_blocked(self, objective_id: str) -> bool:
        if not objective_id:
            return False
        for event in self.store.list_control_events(event_type="human_escalation_required", limit=50):
            payload = dict(event.payload or {})
            if str(payload.get("objective_id") or "") != objective_id:
                continue
            if str(payload.get("reason") or "") != "Three completed coding runs did not advance the objective to a mergeable state.":
                continue
            return True
        return False

    def record_human_escalation(self, reason: str, *, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.store.create_control_recovery_action(
            ControlRecoveryAction(
                id=new_id("recovery"),
                action_type="escalate",
                target_type="system",
                target_id="system",
                reason=reason,
                result="recorded",
            )
        )
        self._record_event("human_escalation_required", "system", "system", payload or {"reason": reason})
        return self.status()

    def status(self) -> dict[str, object]:
        self._expire_cooldowns()
        system = self._reconcile_system_state()
        lane_rows = self.store.list_control_lane_states()
        lanes = {lane.lane_name: lane.state.value for lane in lane_rows}
        active_leases = self.store.list_task_leases()
        cooldowns = [
            {
                "lane": lane.lane_name,
                "until": lane.cooldown_until.isoformat(),
                "reason": lane.reason,
            }
            for lane in lane_rows
            if lane.state == ControlLaneStateValue.COOLDOWN and lane.cooldown_until is not None
        ]
        latest_merge_event = next(
            iter(self.store.list_control_events(limit=1, event_type="merge_succeeded") or self.store.list_control_events(limit=1, event_type="merge_failed")),
            None,
        )
        degraded_lane = next(
            (
                lane
                for lane in lane_rows
                if lane.state in {ControlLaneStateValue.PAUSED, ControlLaneStateValue.COOLDOWN}
            ),
            None,
        )
        return {
            "global_state": system.global_state.value,
            "master_switch": system.master_switch,
            "lanes": lanes,
            "active_task_id": active_leases[0].task_id if active_leases else None,
            "latest_failure_class": degraded_lane.reason if degraded_lane is not None else None,
            "cooldowns": cooldowns,
            "last_merge_status": latest_merge_event.event_type.removeprefix("merge_") if latest_merge_event else None,
            "frozen_reason": system.freeze_reason,
        }

    def _expire_cooldowns(self) -> None:
        now = datetime.now(UTC)
        for lane in self.store.list_control_lane_states():
            if lane.state != ControlLaneStateValue.COOLDOWN or lane.cooldown_until is None:
                continue
            if lane.cooldown_until <= now:
                next_state = ControlLaneStateValue.RUNNING if self.store.get_control_system_state().master_switch else ControlLaneStateValue.PAUSED
                self.store.update_control_lane_state(
                    replace(lane, state=next_state, reason="cooldown_expired", cooldown_until=None, updated_at=now)
                )
                self._record_event("lane_resumed", "lane", lane.lane_name, {"reason": "cooldown_expired", "state": next_state.value})

    def _reconcile_system_state(self) -> ControlSystemState:
        current = self.store.get_control_system_state()
        if current.global_state == GlobalSystemState.FROZEN:
            return current
        if not current.master_switch:
            expected = GlobalSystemState.OFF
            freeze_reason = None
        else:
            lanes = [lane for lane in self.store.list_control_lane_states() if lane.state != ControlLaneStateValue.DISABLED]
            if any(lane.state in {ControlLaneStateValue.PAUSED, ControlLaneStateValue.COOLDOWN} for lane in lanes):
                expected = GlobalSystemState.DEGRADED
                degraded_lane = next(
                    (lane for lane in lanes if lane.state in {ControlLaneStateValue.PAUSED, ControlLaneStateValue.COOLDOWN}),
                    None,
                )
                freeze_reason = degraded_lane.reason if degraded_lane is not None else current.freeze_reason
            elif current.global_state == GlobalSystemState.STARTING:
                expected = GlobalSystemState.HEALTHY
                freeze_reason = None
            else:
                expected = GlobalSystemState.HEALTHY
                freeze_reason = None
        if current.global_state == expected and current.freeze_reason == freeze_reason:
            return current
        updated = replace(current, global_state=expected, freeze_reason=freeze_reason, updated_at=datetime.now(UTC))
        self.store.update_control_system_state(updated)
        return updated

    def _sync_global_state(self, *, reason: str) -> dict[str, object]:
        self._expire_cooldowns()
        current = self._reconcile_system_state()
        if current.global_state == GlobalSystemState.DEGRADED and current.freeze_reason != reason:
            self.store.update_control_system_state(replace(current, freeze_reason=reason, updated_at=datetime.now(UTC)))
        return self.status()

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
