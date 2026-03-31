from __future__ import annotations

from datetime import datetime
import signal
import time

from ..domain import ControlRecoveryAction, new_id
from .common import (
    CLIContext,
    clear_sa_watch_runtime_state,
    clear_sa_watch_launch_state,
    clear_stack_restart_request,
    desired_api_url,
    record_desired_sa_watch_state,
    read_desired_sa_watch_state,
    read_sa_watch_runtime_state,
    emit,
    read_stack_restart_request,
    restart_api_process,
    restart_harness_process,
    start_sa_watch_process,
    startup_preflight,
    stop_sa_watch_process,
    update_sa_watch_runtime_state,
)

SA_WATCH_STARTUP_GRACE_SECONDS = 5.0
SA_WATCH_LOOP_POLL_SECONDS = 1.0
_HEARTBEAT_STALE_SECONDS = 120.0


def handle_control_command(args, ctx: CLIContext) -> bool:
    control_plane = ctx.control_plane
    control_watch = ctx.control_watch
    if args.command == "control-status":
        emit(control_plane.status())
        return True
    if args.command == "control-on":
        emit(control_plane.turn_on())
        return True
    if args.command == "control-off":
        emit(control_plane.turn_off())
        return True
    if args.command == "control-freeze":
        emit(control_plane.freeze(args.reason))
        return True
    if args.command == "control-thaw":
        emit(control_plane.thaw())
        return True
    if args.command == "control-pause-lane":
        emit(control_plane.pause_lane(args.lane_name, reason=args.reason))
        return True
    if args.command == "control-resume-lane":
        emit(control_plane.resume_lane(args.lane_name, reason=args.reason))
        return True
    if args.command == "control-watch-once":
        emit(
            control_watch.run_once(
                api_url=args.api_url or desired_api_url(ctx.config),
                stalled_objective_hours=args.stalled_objective_hours,
                freeze_on_stall=not args.no_freeze_on_stall,
            )
        )
        return True
    if args.command == "control-loop":
        startup_preflight(ctx.config, ctx.store)

        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        control_plane.resume_lane("watch", reason="control_loop_start")
        iteration = 0
        latest = control_plane.status()
        try:
            while True:
                latest = control_watch.run_once(
                    api_url=args.api_url or desired_api_url(ctx.config),
                    stalled_objective_hours=args.stalled_objective_hours,
                    freeze_on_stall=not args.no_freeze_on_stall,
                )
                if bool(latest.get("stuck")):
                    sa_watch_result = ctx.sa_watch.run_once()
                    ctx.store.create_control_recovery_action(
                        ControlRecoveryAction(
                            id=new_id("recovery"),
                            action_type="recover",
                            target_type="system",
                            target_id="system",
                            reason="stuck_detected",
                            result="applied",
                        )
                    )
                    latest = {
                        "mode": "recovered",
                        "stuck_evaluation": latest,
                        "sa_watch": sa_watch_result,
                    }
                restart_request = read_stack_restart_request(ctx.config)
                if restart_request is not None:
                    ctx.store.create_control_recovery_action(
                        ControlRecoveryAction(
                            id=new_id("recovery"),
                            action_type="restart",
                            target_type="system",
                            target_id="system",
                            reason=str(restart_request.get("reason") or "requested"),
                            result="applied",
                        )
                    )
                    clear_stack_restart_request(ctx.config)
                    restart_api_process(ctx.config, force=True)
                    restart_harness_process(ctx.config, force=True)
                iteration += 1
                if args.max_iterations is not None and iteration >= args.max_iterations:
                    break
                time.sleep(max(args.interval_seconds, 0.1))
        finally:
            control_plane.pause_lane("watch", reason="control_loop_exit")
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
        emit(latest)
        return True
    if args.command == "control-sa-watch-once":
        emit(ctx.sa_watch.run_once())
        return True
    if args.command == "sa-watch-start":
        emit(start_sa_watch_process(ctx.config, interval_seconds=args.interval_seconds))
        return True
    if args.command == "sa-watch-stop":
        emit(stop_sa_watch_process(ctx.config))
        return True
    if args.command == "sa-watch-status":
        runtime = read_sa_watch_runtime_state(ctx.config) or {}
        desired = read_desired_sa_watch_state(ctx.config) or {}
        pid = int(runtime.get("pid") or 0)
        pid_alive = pid > 0 and _pid_alive(pid)
        heartbeat_at = float(runtime.get("heartbeat_at") or 0)
        heartbeat_age = max(time.time() - heartbeat_at, 0) if heartbeat_at > 0 else None
        heartbeat_fresh = heartbeat_age is not None and heartbeat_age < _HEARTBEAT_STALE_SECONDS
        healthy = pid_alive and heartbeat_fresh
        emit(
            {
                "desired": desired or None,
                "runtime": runtime or None,
                "running": pid_alive,
                "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
                "heartbeat_fresh": heartbeat_fresh,
                "healthy": healthy,
                "active": _sa_watch_is_active(ctx),
                "log_path": str(ctx.config.db_path.parent / "control" / "sa_watch.log"),
            }
        )
        return True
    if args.command == "sa-watch-loop":
        startup_preflight(ctx.config, ctx.store)
        stop_requested = {"value": False, "signal_count": 0}

        def _request_stop(_signum, _frame):
            stop_requested["signal_count"] += 1
            stop_requested["value"] = True
            if stop_requested["signal_count"] >= 2:
                raise KeyboardInterrupt

        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        if read_desired_sa_watch_state(ctx.config) is None:
            record_desired_sa_watch_state(ctx.config, interval_seconds=args.interval_seconds)
        update_sa_watch_runtime_state(
            ctx.config,
            interval_seconds=args.interval_seconds,
            mode="starting",
            last_reason="booting",
        )
        clear_sa_watch_launch_state(ctx.config)
        print(_sa_watch_line(f"started; interval={args.interval_seconds:.1f}s"), flush=True)
        latest: dict[str, object] = {"mode": "starting"}
        iteration = 0
        previous_kpis: dict[str, int] | None = None
        started_at = time.monotonic()
        next_check_at = started_at + min(max(SA_WATCH_STARTUP_GRACE_SECONDS, 0.0), max(args.interval_seconds, 0.0))
        try:
            while not stop_requested["value"]:
                now_monotonic = time.monotonic()
                desired = read_desired_sa_watch_state(ctx.config)
                if desired is None:
                    update_sa_watch_runtime_state(
                        ctx.config,
                        interval_seconds=args.interval_seconds,
                        mode="stopping",
                        last_reason="desired_state_cleared",
                    )
                    latest = {"mode": "stopping", "reason": "desired_state_cleared"}
                    print(_sa_watch_line("stopping; desired state cleared"), flush=True)
                    break
                if not _sa_watch_is_active(ctx):
                    update_sa_watch_runtime_state(
                        ctx.config,
                        interval_seconds=args.interval_seconds,
                        mode="idle",
                        last_reason="control_plane_or_harness_inactive",
                    )
                    latest = {
                        "mode": "idle",
                        "reason": "control_plane_or_harness_inactive",
                        "control_status": control_plane.status(),
                    }
                    print(_sa_watch_line("idle; waiting for control-plane + harness"), flush=True)
                    current_kpis = _sa_watch_kpis(ctx)
                    print(_sa_watch_workflow_state_line(current_kpis), flush=True)
                    print(_sa_watch_kpi_line(current_kpis, previous_kpis, changed=False), flush=True)
                    previous_kpis = current_kpis
                elif now_monotonic < next_check_at:
                    remaining = max(next_check_at - now_monotonic, 0.0)
                    update_sa_watch_runtime_state(
                        ctx.config,
                        interval_seconds=args.interval_seconds,
                        mode="idle",
                        last_reason="startup_grace_period",
                    )
                    latest = {
                        "mode": "idle",
                        "reason": "startup_grace_period",
                        "seconds_until_first_check": round(remaining, 1),
                        "control_status": control_plane.status(),
                    }
                    print(
                        _sa_watch_line(
                            f"idle; startup grace period ({remaining:.1f}s until first check; waiting for harness to have time to fail or stall)"
                        ),
                        flush=True,
                    )
                    current_kpis = _sa_watch_kpis(ctx)
                    print(_sa_watch_workflow_state_line(current_kpis), flush=True)
                    print(_sa_watch_kpi_line(current_kpis, previous_kpis, changed=False), flush=True)
                    previous_kpis = current_kpis
                else:
                    result = ctx.sa_watch.run_once()
                    report = str(result.get("report") or "") if isinstance(result, dict) else ""
                    update_sa_watch_runtime_state(
                        ctx.config,
                        interval_seconds=args.interval_seconds,
                        mode="active",
                        last_decision="recover" if report else "skip",
                        last_reason=report[:200] if report else "",
                    )
                    latest = {
                        "mode": "active",
                        "result": result,
                    }
                    print(_sa_watch_result_line(result), flush=True)
                    changed = bool(report)
                    current_kpis = _sa_watch_kpis(ctx)
                    print(_sa_watch_workflow_state_line(current_kpis), flush=True)
                    print(_sa_watch_kpi_line(current_kpis, previous_kpis, changed=changed), flush=True)
                    previous_kpis = current_kpis
                    next_check_at = time.monotonic() + max(args.interval_seconds, 0.1)
                iteration += 1
                if args.max_iterations is not None and iteration >= args.max_iterations:
                    break
                sleep_seconds = min(max(args.interval_seconds, 0.1), SA_WATCH_LOOP_POLL_SECONDS)
                time.sleep(sleep_seconds)
        finally:
            clear_sa_watch_runtime_state(ctx.config)
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
            print(_sa_watch_line("stopped"), flush=True)
        emit(latest)
        return True
    return False



def _sa_watch_is_active(ctx: CLIContext) -> bool:
    system = ctx.store.get_control_system_state()
    if not system.master_switch:
        return False
    return bool(ctx.control_watch._running_supervisors())


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import os

        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _sa_watch_line(text: str) -> str:
    return f"{datetime.now().astimezone().strftime('%H:%M:%S')} sa-watch {text}"


def _sa_watch_result_line(result: dict[str, object]) -> str:
    report = str(result.get("report") or "")
    packet = dict(result.get("packet") or {})
    signals = list(packet.get("continuity_signals") or [])
    signal_kinds = ", ".join(str(signal.get("kind") or "unknown") for signal in signals[:3]) or "none"
    action = "recover" if report else "skip"
    summary = report[:120].replace("\n", " ") if report else "no action needed"
    return _sa_watch_line(f"sa-watch: {action}; signals: {signal_kinds}; summary: {summary}")


def _sa_watch_kpis(ctx: CLIContext) -> dict[str, int]:
    tasks = ctx.store.list_tasks()
    objectives = ctx.store.list_objectives()
    stalled_recent = {
        event.entity_id
        for event in ctx.store.list_control_events(event_type="objective_stalled", limit=20)
    }
    return {
        "tasks_completed": sum(1 for task in tasks if getattr(task.status, "value", "") == "completed"),
        "objectives_completed": sum(
            1 for objective in objectives if getattr(objective.status, "value", "") == "resolved"
        ),
        "tasks_pending": sum(1 for task in tasks if getattr(task.status, "value", "") == "pending"),
        "tasks_active": sum(1 for task in tasks if getattr(task.status, "value", "") == "active"),
        "stalled_objectives": sum(
            1
            for objective in objectives
            if objective.id in stalled_recent and getattr(objective.status, "value", "") != "resolved"
        ),
    }


def _sa_watch_forward_progress_increased(
    current_kpis: dict[str, int],
    previous_kpis: dict[str, int] | None,
) -> bool | None:
    if previous_kpis is None:
        return None
    return (
        current_kpis["tasks_completed"] > previous_kpis["tasks_completed"]
        or current_kpis["objectives_completed"] > previous_kpis["objectives_completed"]
        or current_kpis["tasks_pending"] < previous_kpis["tasks_pending"]
        or current_kpis["stalled_objectives"] < previous_kpis["stalled_objectives"]
    )


def _sa_watch_workflow_state_line(current_kpis: dict[str, int]) -> str:
    active = current_kpis.get("tasks_active", 0)
    pending = current_kpis.get("tasks_pending", 0)
    stalled = current_kpis.get("stalled_objectives", 0)
    if stalled > 0 and pending > 0 and active == 0:
        return _sa_watch_line(
            f"workflow state: UNPLUGGED ({stalled} stalled objective"
            f"{'' if stalled == 1 else 's'}, {pending} pending task"
            f"{'' if pending == 1 else 's'}, 0 active)"
        )
    if active > 0:
        return _sa_watch_line(f"workflow state: FLOWING ({active} active task{'' if active == 1 else 's'})")
    if pending > 0:
        return _sa_watch_line(f"workflow state: READY BUT NOT RUNNING ({pending} pending task{'' if pending == 1 else 's'})")
    return _sa_watch_line("workflow state: IDLE (no pending or active tasks)")


def _sa_watch_kpi_line(
    current_kpis: dict[str, int],
    previous_kpis: dict[str, int] | None,
    *,
    changed: bool,
) -> str:
    forward = _sa_watch_forward_progress_increased(current_kpis, previous_kpis)
    if forward is None:
        progress_text = "unknown (first cycle)"
    else:
        progress_text = "yes" if forward else "no"
    changed_text = "yes" if changed else "no"
    completed_delta = _sa_watch_delta(current_kpis, previous_kpis, "tasks_completed")
    objectives_delta = _sa_watch_delta(current_kpis, previous_kpis, "objectives_completed")
    pending_delta = _sa_watch_delta(current_kpis, previous_kpis, "tasks_pending")
    active_delta = _sa_watch_delta(current_kpis, previous_kpis, "tasks_active")
    stalled_delta = _sa_watch_delta(current_kpis, previous_kpis, "stalled_objectives")
    return _sa_watch_line(
        "summary: "
        f"totals [tasks completed {current_kpis['tasks_completed']}, "
        f"objectives completed {current_kpis['objectives_completed']}, "
        f"pending {current_kpis['tasks_pending']}, "
        f"active {current_kpis['tasks_active']}, "
        f"stalled objectives {current_kpis['stalled_objectives']}]; "
        f"deltas [tasks completed {completed_delta}, "
        f"objectives completed {objectives_delta}, "
        f"pending {pending_delta}, "
        f"active {active_delta}, "
        f"stalled objectives {stalled_delta}]; "
        f"forward progress: {progress_text}; "
        f"changed code/workflow: {changed_text}"
    )


def _sa_watch_changed_anything(result: dict[str, object]) -> bool:
    return bool(result.get("report"))


def _sa_watch_delta(
    current_kpis: dict[str, int],
    previous_kpis: dict[str, int] | None,
    key: str,
) -> str:
    if previous_kpis is None:
        return "n/a"
    delta = current_kpis[key] - previous_kpis[key]
    return f"{delta:+d}"


def _sa_watch_action_label(action: str) -> str:
    labels = {
        "none": "observe only",
        "record_escalation": "note concern",
        "model_response_unusable": "could not make a trustworthy decision",
        "resume_worker": "resume worker",
        "restart_stack": "restart stack",
        "freeze_system": "freeze system",
        "repair_workflow_state": "repair workflow state directly",
        "repair_harness": "repair harness directly",
        "skip": "skip",
    }
    return labels.get(action, action.replace("_", " "))


def _sa_watch_reason_text(reason: str) -> str:
    if reason.strip().lower() in {"", "sa-watch returned no reason"}:
        return "unavailable"
    return reason


def _sa_watch_effect_line(effect: dict[str, object]) -> str:
    kind = str(effect.get("kind") or "effect")
    if kind == "stack_restart_requested":
        return _sa_watch_line(f"requested stack restart; reason={effect.get('reason')}")
    if kind == "lane_resumed":
        return _sa_watch_line(f"resumed lane {effect.get('lane')}; reason={effect.get('reason')}")
    if kind == "system_frozen":
        return _sa_watch_line(f"froze system; reason={effect.get('reason')}")
    if kind == "repair_validated":
        return _sa_watch_line(f"validated direct harness repair {effect.get('run_id')}")
    if kind == "repair_failed":
        return _sa_watch_line(f"direct harness repair failed; reason={effect.get('reason')}")
    if kind == "workflow_state_repaired":
        return _sa_watch_line(
            f"repaired workflow state for objective {effect.get('objective_id')}; "
            f"ignored={len(list(effect.get('ignored_task_ids') or []))}; "
            f"waived={len(list(effect.get('waived_task_ids') or []))}"
        )
    if kind == "noted_concern":
        return _sa_watch_line(
            f"noted concern; no code/workflow change made; reason={_sa_watch_reason_text(str(effect.get('reason') or ''))}"
        )
    if kind == "observed":
        return _sa_watch_line(
            f"observed only; no code/workflow change made; reason={_sa_watch_reason_text(str(effect.get('reason') or ''))}"
        )
    if kind == "model_response_unusable":
        return _sa_watch_line("could not make a trustworthy decision; no additional action taken")
    return _sa_watch_line(f"{kind}: {effect}")
