from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

from ..config import HarnessConfig, default_config_path, write_persisted_config
from ..domain import Event, PromotionMode, RepoProvider, WorkspacePolicy, new_id, serialize_dataclass
from ..onboarding import detect_llm_command_candidates, doctor_report, probe_llm_command, prompt_text
from .common import CLIContext, emit, ensure_llm_ready


def _redact_command(value: str | None) -> str | None:
    if not value:
        return None
    command = value.strip()
    if not command:
        return None
    first = command.split()[0]
    return f"{first} [REDACTED]"


def _task_scope_from_args(args) -> dict[str, object]:
    scope: dict[str, object] = {}
    if getattr(args, "allowed_paths", None):
        scope["allowed_paths"] = list(args.allowed_paths)
    if getattr(args, "forbidden_paths", None):
        scope["forbidden_paths"] = list(args.forbidden_paths)
    return scope


def _get_or_create_smoke_project(engine, store, project_name: str):
    for project in store.list_projects():
        if project.name == project_name:
            return project
    return engine.create_project(project_name, "Local smoke-test project", adapter_name="generic")


def _serialize_heartbeat_result(result):
    return asdict(result)


def _readiness_label(value: bool) -> str:
    return "yes" if value else "no"


def _doctor_text(payload: dict[str, object]) -> str:
    prototype = payload["prototype"]
    config_file = payload["config_file"]
    database = payload["database"]
    llm = payload["llm"]
    readiness = payload["readiness"]
    detected = [
        item["label"]
        for item in llm["detected_candidates"]
        if item["available"]
    ]
    selected_backend = llm.get("selected_backend") or "none"
    issues = payload.get("issues") or []
    recommendations = payload.get("recommendations") or []
    next_steps = payload.get("next_steps") or []
    lines = [
        "Accruvia Harness doctor",
        f"Stage: {prototype['stage']}",
        f"Warning: {prototype['warning']}",
        "",
        "State",
        f"- Harness home: {payload['harness_home']}",
        f"- Config file: {'found' if config_file['exists'] else 'missing'} ({config_file['path']})",
        f"- Database: {'found' if database['exists'] else 'missing'} ({database['path']})",
        "",
        "Providers",
        f"- Detected on PATH: {', '.join(detected) if detected else 'none'}",
        f"- Selected provider: {selected_backend}",
        f"- Configured executors: {', '.join(llm['configured_executors']) if llm['configured_executors'] else 'none'}",
        "",
        "Readiness",
        f"- Inspection: {_readiness_label(readiness['inspection_ready'])}",
        f"- Task execution: {_readiness_label(readiness['task_execution_ready'])}",
        f"- Heartbeats: {_readiness_label(readiness['heartbeats_ready'])}",
        f"- Autonomous watch mode: {_readiness_label(readiness['autonomous_ready'])}",
    ]
    if issues:
        lines.extend(["", "Issues"])
        lines.extend(f"- {item}" for item in issues)
    if recommendations:
        lines.extend(["", "Recommendations"])
        lines.extend(f"- {item}" for item in recommendations)
    if next_steps:
        lines.extend(["", "Next steps"])
        lines.extend(f"- {item}" for item in next_steps)
    return "\n".join(lines)


def _smoke_test_text(payload: dict[str, object]) -> str:
    project = payload["project"]
    task = payload["task"]
    runs = payload.get("runs") or []
    events = payload.get("events") or []
    lines = [
        "Smoke test complete",
        "",
        "Project",
        f"- {project['name']} ({project['id']})",
        "",
        "Task",
        f"- {task['title']} ({task['id']})",
        f"- Status: {task['status']}",
        "",
        "Activity",
        f"- Runs recorded: {len(runs)}",
        f"- Events recorded: {len(events)}",
        "",
        "Next step",
        "- Use `./bin/accruvia-harness supervise` to keep the machine running.",
    ]
    return "\n".join(lines)


def _backlog_delta_text(before: dict[str, object] | None, after: dict[str, object] | None) -> str | None:
    if not before or not after:
        return None
    before_status = dict(before.get("tasks_by_status") or {})
    after_status = dict(after.get("tasks_by_status") or {})
    keys = sorted(set(before_status) | set(after_status))
    parts: list[str] = []
    for key in keys:
        previous = int(before_status.get(key, 0) or 0)
        current = int(after_status.get(key, 0) or 0)
        if previous == current:
            continue
        delta = current - previous
        sign = "+" if delta > 0 else ""
        parts.append(f"{key} {previous}->{current} ({sign}{delta})")
    previous_promotions = int(before.get("pending_promotions", 0) or 0)
    current_promotions = int(after.get("pending_promotions", 0) or 0)
    if previous_promotions != current_promotions:
        delta = current_promotions - previous_promotions
        sign = "+" if delta > 0 else ""
        parts.append(f"pending_promotions {previous_promotions}->{current_promotions} ({sign}{delta})")
    if not parts:
        return "no backlog change"
    return ", ".join(parts)


def _supervise_start_text(
    *,
    project_id: str | None,
    watch: bool,
    worker_id: str,
    heartbeat_project_ids: list[str],
    heartbeat_all_projects: bool,
    review_check_enabled: bool,
) -> str:
    scope = project_id or "all projects"
    mode = "continuous" if watch else "one-shot"
    heartbeat_scope = "all projects" if heartbeat_all_projects else ", ".join(heartbeat_project_ids) if heartbeat_project_ids else "disabled"
    review_checks = "enabled" if review_check_enabled else "disabled"
    return "\n".join(
        [
            "Supervisor started",
            f"- Scope: {scope}",
            f"- Mode: {mode}",
            f"- Worker: {worker_id}",
            f"- Heartbeats: {heartbeat_scope}",
            f"- Review checks: {review_checks}",
            "Waiting for work...",
        ]
    )


def _emit_supervise_progress(event: dict[str, object]) -> None:
    event_type = str(event.get("type"))
    if event_type == "task_started":
        print(f"Started task {event['task_title']} ({event['task_id']})", flush=True)
        return
    if event_type == "task_finished":
        print(
            f"Task {event['task_title']} is now {event['status']} ({event['task_id']})",
            flush=True,
        )
        summary = str(event.get("summary") or "").strip()
        if summary:
            print(f"  Summary: {summary}", flush=True)
        backlog_delta = _backlog_delta_text(event.get("backlog_before"), event.get("backlog_after"))
        if backlog_delta:
            print(f"  Backlog delta: {backlog_delta}", flush=True)
        return
    if event_type == "task_processed":
        print(f"Processed task {event['task_title']} ({event['processed_count']} total)", flush=True)
        return
    if event_type == "heartbeat_succeeded":
        print(
            f"Heartbeat succeeded for {event['project_id']} ({event['heartbeat_count']} total, created {event['created_task_count']} tasks)",
            flush=True,
        )
        summary = str(event.get("summary") or "").strip()
        if summary:
            print(f"  Summary: {summary}", flush=True)
        backlog_delta = _backlog_delta_text(event.get("backlog_before"), event.get("backlog_after"))
        if backlog_delta:
            print(f"  Backlog delta: {backlog_delta}", flush=True)
        return
    if event_type == "heartbeat_failed":
        print(
            f"Heartbeat failed for {event['project_id']} (attempt {event['consecutive_failures']}): {event['message']}",
            flush=True,
        )
        return
    if event_type == "heartbeat_escalated":
        print(
            f"Heartbeat escalated for {event['project_id']} after {event['consecutive_failures']} failures",
            flush=True,
        )
        return
    if event_type == "heartbeat_disabled":
        print(
            f"Heartbeats disabled for {event['project_id']} after {event['consecutive_failures']} failures",
            flush=True,
        )
        return
    if event_type == "review_checked":
        print(
            f"Review check ran: checked {event['checked_count']}, conflicts {event['conflict_count']}, merged {event['merged_count']}",
            flush=True,
        )
        return
    if event_type == "sleeping":
        print(f"Idle. Sleeping {event['seconds']}s (idle cycle {event['idle_cycles']})", flush=True)
        return


def _supervise_summary_text(result) -> str:
    return "\n".join(
        [
            "Supervisor stopped",
            f"- Exit reason: {result.exit_reason}",
            f"- Tasks processed: {result.processed_count}",
            f"- Heartbeats run: {result.heartbeat_count}",
            f"- Review checks: {result.review_check_count}",
            f"- Idle cycles: {result.idle_cycles}",
            f"- Slept seconds: {result.slept_seconds}",
        ]
    )


def _resolved_config_file(args, config) -> Path:
    if getattr(args, "config_file", None):
        return Path(args.config_file)
    return default_config_path(config.db_path.parent)


def _updated_config_payload(config: HarnessConfig, updates: dict[str, object], *, clear_existing: bool = False) -> dict[str, object]:
    payload = {} if clear_existing else config.persisted_payload()
    payload.update(updates)
    return payload


def _persist_config_updates(
    args,
    config: HarnessConfig,
    updates: dict[str, object],
    *,
    clear_existing: bool = False,
) -> Path:
    config_path = _resolved_config_file(args, config)
    payload = _updated_config_payload(config, updates, clear_existing=clear_existing)
    return write_persisted_config(config_path, payload)


def _parse_env_passthrough(raw: str | None, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return fallback
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _backend_command_key(backend: str) -> str:
    return {
        "command": "llm_command",
        "codex": "llm_codex_command",
        "claude": "llm_claude_command",
        "accruvia_client": "llm_accruvia_client_command",
    }[backend]


def _run_setup(args, config: HarnessConfig) -> dict[str, object]:
    candidates = detect_llm_command_candidates()
    available = [item for item in candidates if item.available]
    selection = None
    command_updates: dict[str, object] = {}
    sys.stderr.write("Accruvia Harness setup\n")
    sys.stderr.write("This prototype needs at least one working LLM provider for heartbeats, explanations, and autonomous project work.\n")
    sys.stderr.write(f"Persisted config file: {_resolved_config_file(args, config)}\n")
    if args.custom_command:
        selection = {"backend": "command", "command": args.custom_command, "label": "Custom command"}
    else:
        if available:
            sys.stderr.write("Installed providers detected on PATH:\n")
            for index, item in enumerate(available, start=1):
                sys.stderr.write(f"  {index}. {item.label} -> {item.command}\n")
        else:
            sys.stderr.write("No known supported LLM CLIs were detected on PATH.\n")
        default_choice = "1" if available else "c"
        choice = default_choice if args.yes else prompt_text(
            "Select the provider you want to configure (`1`, `2`, ..., `c` for custom, `q` to cancel setup)",
            default=default_choice,
        )
        if choice.lower() == "q":
            selection = None
        elif choice.lower() == "c":
            custom_command = prompt_text(
                "Enter the shell command the harness should use for LLM calls",
                default=args.custom_command or config.llm_command or "",
            )
            if custom_command:
                selection = {"backend": "command", "command": custom_command, "label": "Custom command"}
        else:
            selected = available[int(choice) - 1]
            selection = {"backend": selected.backend, "command": selected.command, "label": selected.label}
    if selection is not None:
        selected_backend = str(selection["backend"])
        command_updates[_backend_command_key(selected_backend)] = str(selection["command"])
        if not args.skip_other_detected:
            for item in available:
                command_updates[_backend_command_key(item.backend)] = item.command
        preferred_backend = selected_backend if args.yes else prompt_text(
            "Preferred backend (`auto`, `command`, `codex`, `claude`)",
            default=selected_backend,
        )
        command_updates["llm_backend"] = preferred_backend
        command_updates["env_passthrough"] = config.env_passthrough
    config_path = _persist_config_updates(args, config, command_updates)
    resolved_config = HarnessConfig.from_env(args.db, args.workspace, args.log_path, args.config_file)
    report = doctor_report(resolved_config, config_path=config_path)
    probe = probe_llm_command(str(selection["command"])) if selection is not None else None
    return {
        "prototype_warning": "Prototype mode: run doctor and smoke-test before enabling autonomous heartbeats.",
        "configured": selection is not None,
        "selected": selection,
        "probe": probe,
        "config_file": str(config_path),
        "doctor": report,
        "next_steps": [
            "Run `./bin/accruvia-harness doctor` to inspect readiness levels.",
            "Run `./bin/accruvia-harness smoke-test` before enabling autonomous heartbeats.",
            "Use `./bin/accruvia-harness supervise --one-shot` before long-running watch mode.",
        ],
    }


def _emit_setup_result(payload: dict[str, object]) -> None:
    if not sys.stdout.isatty():
        emit(payload)
        return
    print("Setup complete.")
    print(payload["prototype_warning"])
    selected = payload.get("selected")
    if selected:
        print(f"Selected provider: {selected['label']}")
        probe = payload.get("probe") or {}
        if probe.get("ok"):
            print(f"{selected['label']} is configured and working.")
            preview = probe.get("response_preview")
            if preview:
                print(f"Probe response preview: {preview}")
        else:
            print(f"{selected['label']} was saved, but the smoke check failed: {probe.get('message')}")
            print("You may need to finish CLI login or adjust the command before using heartbeats.")
    else:
        print("No provider was configured during setup.")
    print(f"Config saved to: {payload['config_file']}")
    print("Next steps:")
    for step in payload.get("next_steps", []):
        print(f"- {step}")


def _reset_local_state(args, config: HarnessConfig) -> dict[str, object]:
    if not args.yes:
        raise ValueError("reset-local-state is destructive; rerun with --yes to confirm.")
    home = config.db_path.parent
    config_path = _resolved_config_file(args, config)
    removed: list[str] = []
    preserved: list[str] = []
    if home.exists():
        for child in sorted(home.iterdir()):
            if args.keep_config and child.resolve() == config_path.resolve():
                preserved.append(str(child))
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except FileNotFoundError:
                continue
            removed.append(str(child))
    home.mkdir(parents=True, exist_ok=True)
    if args.keep_config and config_path.exists():
        preserved.append(str(config_path))
    return {
        "reset": True,
        "prototype_warning": "Prototype reset completed. Re-run setup and smoke-test before resuming autonomy.",
        "state_root": str(home),
        "removed": removed,
        "preserved": preserved,
        "next_steps": [
            "Run `./bin/accruvia-harness setup` to restore persisted operator settings.",
            "Run `./bin/accruvia-harness init-db` to recreate the database.",
            "Run `./bin/accruvia-harness smoke-test` before enabling supervision.",
        ],
    }


def _build_supervise_restart_command(record: dict[str, object]) -> list[str]:
    command = [sys.executable, "-m", "accruvia_harness", "supervise"]
    project_id = record.get("project_id")
    if project_id:
        command.extend(["--project-id", str(project_id)])
    worker_id = record.get("worker_id")
    if worker_id:
        command.extend(["--worker-id", str(worker_id)])
    lease_seconds = record.get("lease_seconds")
    if lease_seconds is not None:
        command.extend(["--lease-seconds", str(lease_seconds)])
    if not bool(record.get("watch", True)):
        command.append("--one-shot")
    idle_sleep_seconds = record.get("idle_sleep_seconds")
    if idle_sleep_seconds is not None:
        command.extend(["--idle-sleep-seconds", str(idle_sleep_seconds)])
    max_idle_cycles = record.get("max_idle_cycles")
    if max_idle_cycles is not None:
        command.extend(["--max-idle-cycles", str(max_idle_cycles)])
    max_iterations = record.get("max_iterations")
    if max_iterations is not None:
        command.extend(["--max-iterations", str(max_iterations)])
    for heartbeat_project_id in list(record.get("heartbeat_project_ids") or []):
        command.extend(["--heartbeat-project-id", str(heartbeat_project_id)])
    heartbeat_interval_seconds = record.get("heartbeat_interval_seconds")
    if heartbeat_interval_seconds is not None:
        command.extend(["--heartbeat-interval-seconds", str(heartbeat_interval_seconds)])
    if bool(record.get("heartbeat_all_projects", False)):
        command.append("--heartbeat-all-projects")
    if bool(record.get("review_check_enabled", False)):
        command.append("--review-check-enabled")
    review_check_interval_seconds = record.get("review_check_interval_seconds")
    if review_check_interval_seconds is not None:
        command.extend(["--review-check-interval-seconds", str(review_check_interval_seconds)])
    return command


def _restart_supervisor_process(config, record: dict[str, object]) -> dict[str, object]:
    control_dir = config.db_path.parent / "supervisors"
    control_dir.mkdir(parents=True, exist_ok=True)
    worker_id = str(record.get("worker_id") or "supervisor")
    project_id = str(record.get("project_id") or "all-projects")
    restart_log_path = control_dir / f"restart_{project_id}_{worker_id}.log"
    command = _build_supervise_restart_command(record)
    with restart_log_path.open("ab") as handle:
        process = subprocess.Popen(
            command,
            cwd=str(Path.cwd()),
            env=os.environ.copy(),
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    return {
        "pid": process.pid,
        "project_id": record.get("project_id"),
        "worker_id": record.get("worker_id"),
        "command": command,
        "restart_log_path": str(restart_log_path),
    }


def _supervisor_control_dir(config) -> str:
    return str(config.db_path.parent / "supervisors")


def _supervisor_stop_request_path(config) -> str:
    return str(config.db_path.parent / "supervisors" / "stop.request")


def _supervisor_pid_path(config, pid: int) -> str:
    return str(config.db_path.parent / "supervisors" / f"{pid}.json")


def _list_supervisor_records(config) -> list[dict[str, object]]:
    control_dir = config.db_path.parent / "supervisors"
    if not control_dir.exists():
        return []
    records: list[dict[str, object]] = []
    for path in sorted(control_dir.glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return records


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _prune_supervisor_records(config) -> list[dict[str, object]]:
    control_dir = config.db_path.parent / "supervisors"
    records = _list_supervisor_records(config)
    alive: list[dict[str, object]] = []
    for record in records:
        pid = int(record.get("pid", 0))
        if pid > 0 and _pid_is_alive(pid):
            alive.append(record)
            continue
        pid_path = control_dir / f"{pid}.json"
        if pid_path.exists():
            pid_path.unlink()
    return alive


def handle_core_command(args, ctx: CLIContext) -> bool:
    config = ctx.config
    store = ctx.store
    engine = ctx.engine
    if args.command == "init-db":
        emit({"db": str(config.db_path), "initialized": True, "schema_version": store.schema_version(), "expected_schema_version": store.expected_schema_version()})
        return True
    if args.command == "reset-local-state":
        emit(_reset_local_state(args, config))
        return True
    if args.command == "config":
        emit({
            "config_file_path": str(_resolved_config_file(args, config)),
            "config_file_exists": _resolved_config_file(args, config).exists(),
            "db_path": str(config.db_path),
            "workspace_root": str(config.workspace_root),
            "log_path": str(config.log_path),
            "telemetry_dir": str(config.telemetry_dir),
            "default_project_name": config.default_project_name,
            "default_repo": config.default_repo,
            "runtime_backend": config.runtime_backend,
            "temporal_target": config.temporal_target,
            "temporal_namespace": config.temporal_namespace,
            "temporal_task_queue": config.temporal_task_queue,
            "worker_backend": config.worker_backend,
            "worker_command": _redact_command(config.worker_command),
            "llm_backend": config.llm_backend,
            "llm_model": config.llm_model,
            "llm_command": _redact_command(config.llm_command),
            "llm_codex_command": _redact_command(config.llm_codex_command),
            "llm_claude_command": _redact_command(config.llm_claude_command),
            "llm_accruvia_client_command": _redact_command(config.llm_accruvia_client_command),
            "env_passthrough": list(config.env_passthrough),
            "adapter_modules": list(config.adapter_modules),
            "project_adapter_modules": list(config.project_adapter_modules),
            "validator_modules": list(config.validator_modules),
            "cognition_modules": list(config.cognition_modules),
            "timeout_ema_alpha": config.timeout_ema_alpha,
            "timeout_min_seconds": config.timeout_min_seconds,
            "timeout_max_seconds": config.timeout_max_seconds,
            "timeout_multiplier": config.timeout_multiplier,
            "heartbeat_timeout_seconds": config.heartbeat_timeout_seconds,
            "heartbeat_failure_escalation_threshold": config.heartbeat_failure_escalation_threshold,
            "memory_limit_mb": config.memory_limit_mb,
            "cpu_time_limit_seconds": config.cpu_time_limit_seconds,
            "observer_webhook_url": config.observer_webhook_url,
            "default_workspace_policy": config.default_workspace_policy,
            "default_promotion_mode": config.default_promotion_mode,
            "default_repo_provider": config.default_repo_provider,
            "default_base_branch": config.default_base_branch,
            "pr_check_enabled": config.pr_check_enabled,
            "pr_check_interval_seconds": config.pr_check_interval_seconds,
        })
        return True
    if args.command == "doctor":
        payload = doctor_report(config, config_path=_resolved_config_file(args, config))
        if args.json:
            emit(payload)
        else:
            print(_doctor_text(payload))
        return True
    if args.command == "setup":
        _emit_setup_result(_run_setup(args, config))
        return True
    if args.command == "configure-llm":
        updates: dict[str, object] = {}
        if args.backend:
            updates["llm_backend"] = args.backend
        if args.model is not None:
            updates["llm_model"] = args.model
        if args.llm_command_value is not None:
            updates["llm_command"] = args.llm_command_value
        if args.llm_codex_command_value is not None:
            updates["llm_codex_command"] = args.llm_codex_command_value
        if args.llm_claude_command_value is not None:
            updates["llm_claude_command"] = args.llm_claude_command_value
        if args.llm_accruvia_client_command_value is not None:
            updates["llm_accruvia_client_command"] = args.llm_accruvia_client_command_value
        if args.env_passthrough is not None:
            updates["env_passthrough"] = tuple(args.env_passthrough)
        config_path = _persist_config_updates(args, config, updates, clear_existing=args.clear_existing)
        resolved_config = HarnessConfig.from_env(args.db, args.workspace, args.log_path, args.config_file)
        emit(
            {
                "config_file": str(config_path),
                "saved": True,
                "llm": {
                    "backend": resolved_config.llm_backend,
                    "llm_command": _redact_command(resolved_config.llm_command),
                    "llm_codex_command": _redact_command(resolved_config.llm_codex_command),
                    "llm_claude_command": _redact_command(resolved_config.llm_claude_command),
                    "llm_accruvia_client_command": _redact_command(resolved_config.llm_accruvia_client_command),
                    "env_passthrough": list(resolved_config.env_passthrough),
                },
                "doctor": doctor_report(resolved_config, config_path=config_path),
            }
        )
        return True
    if args.command == "create-project":
        project = engine.create_project(
            args.name,
            args.description,
            adapter_name=args.adapter_name,
            workspace_policy=WorkspacePolicy(
                args.workspace_policy or config.default_workspace_policy
            ),
            promotion_mode=PromotionMode(
                args.promotion_mode or config.default_promotion_mode
            ),
            repo_provider=RepoProvider(args.repo_provider or config.default_repo_provider)
            if (args.repo_provider or config.default_repo_provider)
            else None,
            repo_name=args.repo_name or config.default_repo,
            base_branch=args.base_branch or config.default_base_branch,
        )
        payload = {"project": serialize_dataclass(project)}
        if not args.no_bootstrap_heartbeat:
            try:
                ensure_llm_ready(args, ctx, reason="Project bootstrap heartbeat")
                payload["heartbeat"] = _serialize_heartbeat_result(engine.heartbeat(project.id))
            except Exception as exc:
                payload["heartbeat_error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
        emit(payload)
        return True
    if args.command == "update-project":
        emit(
            {
                "project": serialize_dataclass(
                    engine.update_project(
                        args.project_id,
                        name=args.name,
                        description=args.description,
                        adapter_name=args.adapter_name,
                        workspace_policy=WorkspacePolicy(args.workspace_policy) if args.workspace_policy else None,
                        promotion_mode=PromotionMode(args.promotion_mode) if args.promotion_mode else None,
                        repo_provider=RepoProvider(args.repo_provider) if args.repo_provider else None,
                        repo_name=args.repo_name,
                        base_branch=args.base_branch,
                        max_concurrent_tasks=args.max_concurrent_tasks,
                    )
                )
            }
        )
        return True
    if args.command == "create-task":
        required_artifacts = args.required_artifacts or ["plan", "report"]
        scope = _task_scope_from_args(args)
        task = engine.create_task_with_policy(
            project_id=args.project_id,
            title=args.title,
            objective=args.objective,
            priority=args.priority,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=args.external_ref_type,
            external_ref_id=args.external_ref_id,
            validation_profile=args.validation_profile,
            scope=scope,
            strategy=args.strategy,
            max_attempts=args.max_attempts,
            max_branches=args.max_branches,
            required_artifacts=required_artifacts,
        )
        emit({"task": serialize_dataclass(task)})
        return True
    if args.command == "run-once":
        run = engine.run_once(args.task_id)
        emit({"run": serialize_dataclass(run), "artifacts": [serialize_dataclass(i) for i in store.list_artifacts(run.id)], "evaluations": [serialize_dataclass(i) for i in store.list_evaluations(run.id)], "decisions": [serialize_dataclass(i) for i in store.list_decisions(run.id)]})
        return True
    if args.command == "run-until-stable":
        runs = engine.run_until_stable(args.task_id)
        emit({"runs": [serialize_dataclass(r) for r in runs], "task": serialize_dataclass(store.get_task(args.task_id))})
        return True
    if args.command == "process-next":
        result = engine.process_next_task(args.project_id, worker_id=args.worker_id, lease_seconds=args.lease_seconds)
        emit({"processed": None} if result is None else {"task": serialize_dataclass(result["task"]), "runs": [serialize_dataclass(r) for r in result["runs"]]})
        return True
    if args.command == "process-queue":
        results = engine.process_queue(args.limit, args.project_id, worker_id=args.worker_id, lease_seconds=args.lease_seconds)
        emit({"processed": [{"task": serialize_dataclass(i["task"]), "runs": [serialize_dataclass(r) for r in i["runs"]]} for i in results]})
        return True
    if args.command == "stop-supervisors":
        control_dir = config.db_path.parent / "supervisors"
        control_dir.mkdir(parents=True, exist_ok=True)
        stop_path = control_dir / "stop.request"
        stop_path.write_text("graceful-stop-requested\n", encoding="utf-8")
        emit(
            {
                "stop_requested": True,
                "stop_request_path": str(stop_path),
                "running_supervisors": _prune_supervisor_records(config),
            }
        )
        return True
    if args.command == "kill-supervisors":
        killed: list[dict[str, object]] = []
        for record in _prune_supervisor_records(config):
            pid = int(record.get("pid", 0))
            if pid <= 0:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(record)
            except OSError:
                continue
        emit({"killed_supervisors": killed, "count": len(killed)})
        return True
    if args.command == "supervise":
        max_idle_cycles = args.max_idle_cycles
        if max_idle_cycles is None:
            max_idle_cycles = None if args.watch else 1
        heartbeat_project_ids = list(args.heartbeat_project_ids or [])
        heartbeat_interval_seconds = args.heartbeat_interval_seconds
        if args.project_id and not args.heartbeat_all_projects and not heartbeat_project_ids:
            heartbeat_project_ids = [args.project_id]
        if heartbeat_project_ids and heartbeat_interval_seconds is None:
            heartbeat_interval_seconds = 1800.0
        if heartbeat_project_ids or args.heartbeat_all_projects:
            config = ensure_llm_ready(args, ctx, reason="Supervisor heartbeats")
        control_dir = config.db_path.parent / "supervisors"
        control_dir.mkdir(parents=True, exist_ok=True)
        stop_request_path = control_dir / "stop.request"
        if stop_request_path.exists():
            stop_request_path.unlink()
        stop_requested = {"value": False, "signal_count": 0}

        def _request_stop(_signum, _frame):
            stop_requested["signal_count"] += 1
            stop_requested["value"] = True
            stop_request_path.write_text("graceful-stop-requested\n", encoding="utf-8")
            if stop_requested["signal_count"] >= 2:
                raise KeyboardInterrupt

        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        pid = os.getpid()
        pid_path = control_dir / f"{pid}.json"
        pid_path.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "worker_id": args.worker_id,
                    "project_id": args.project_id,
                    "command": "supervise",
                    "watch": args.watch,
                    "lease_seconds": args.lease_seconds,
                    "idle_sleep_seconds": args.idle_sleep_seconds,
                    "max_idle_cycles": max_idle_cycles,
                    "max_iterations": args.max_iterations,
                    "heartbeat_project_ids": heartbeat_project_ids,
                    "heartbeat_interval_seconds": heartbeat_interval_seconds,
                    "heartbeat_all_projects": args.heartbeat_all_projects,
                    "review_check_enabled": args.review_check_enabled or config.pr_check_enabled,
                    "review_check_interval_seconds": args.review_check_interval_seconds or config.pr_check_interval_seconds,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        if not args.json:
            print(
                _supervise_start_text(
                    project_id=args.project_id,
                    watch=args.watch,
                    worker_id=args.worker_id,
                    heartbeat_project_ids=heartbeat_project_ids,
                    heartbeat_all_projects=args.heartbeat_all_projects,
                    review_check_enabled=args.review_check_enabled or config.pr_check_enabled,
                ),
                flush=True,
            )
        try:
            result = engine.supervise(
                project_id=args.project_id,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
                watch=args.watch,
                idle_sleep_seconds=args.idle_sleep_seconds,
                max_idle_cycles=max_idle_cycles,
                max_iterations=args.max_iterations,
                heartbeat_project_ids=heartbeat_project_ids or None,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                heartbeat_all_projects=args.heartbeat_all_projects,
                review_check_enabled=args.review_check_enabled or config.pr_check_enabled,
                review_check_interval_seconds=args.review_check_interval_seconds or config.pr_check_interval_seconds,
                stop_requested=lambda: stop_requested["value"] or stop_request_path.exists(),
                progress_callback=None if args.json else _emit_supervise_progress,
            )
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
            if pid_path.exists():
                pid_path.unlink()
            if stop_request_path.exists() and not _prune_supervisor_records(config):
                stop_request_path.unlink()
        if args.json:
            emit(serialize_dataclass(result))
        else:
            print(_supervise_summary_text(result))
        return True
    if args.command == "nudge-project":
        project = store.get_project(args.project_id)
        if project is None:
            raise ValueError(f"Unknown project: {args.project_id}")
        nudge_event = Event(
            id=new_id("event"),
            entity_type="project",
            entity_id=args.project_id,
            event_type="operator_nudge",
            payload={
                "note": args.note,
                "author": os.environ.get("USER") or os.environ.get("USERNAME") or "operator",
            },
        )
        store.create_event(nudge_event)

        restarted_supervisors: list[dict[str, object]] = []
        matching_supervisors = [
            record
            for record in _prune_supervisor_records(config)
            if record.get("project_id") == args.project_id
        ]
        if matching_supervisors and not args.no_restart_running_supervisors:
            control_dir = config.db_path.parent / "supervisors"
            control_dir.mkdir(parents=True, exist_ok=True)
            stop_path = control_dir / "stop.request"
            stop_path.write_text("graceful-stop-requested\n", encoding="utf-8")
            deadline = time.time() + max(args.restart_timeout_seconds, 1)
            while time.time() < deadline:
                remaining = [
                    record
                    for record in _prune_supervisor_records(config)
                    if record.get("project_id") == args.project_id
                ]
                if not remaining:
                    break
                time.sleep(1.0)
            remaining = [
                record
                for record in _prune_supervisor_records(config)
                if record.get("project_id") == args.project_id
            ]
            if remaining:
                raise RuntimeError(
                    f"Timed out waiting for supervisors to stop for project {args.project_id}"
                )

        payload: dict[str, object] = {
            "nudge": serialize_dataclass(nudge_event),
            "restarted_supervisors": restarted_supervisors,
        }
        if not args.no_heartbeat:
            ensure_llm_ready(args, ctx, reason="Project nudge heartbeat")
            heartbeat = engine.heartbeat(args.project_id)
            payload["heartbeat"] = _serialize_heartbeat_result(heartbeat)
            if heartbeat.created_tasks and not args.no_process_created_tasks:
                payload["processing"] = serialize_dataclass(
                    engine.supervise(
                        project_id=args.project_id,
                        worker_id=args.worker_id,
                        lease_seconds=args.lease_seconds,
                        watch=False,
                        max_idle_cycles=1,
                    )
                )
        if matching_supervisors and not args.no_restart_running_supervisors:
            for record in matching_supervisors:
                restarted_supervisors.append(_restart_supervisor_process(config, record))
        emit(payload)
        return True
    if args.command == "check-reviews":
        emit(
            serialize_dataclass(
                engine.check_reviews(args.interval_seconds or config.pr_check_interval_seconds)
            )
        )
        return True
    if args.command == "review-promotion":
        result = engine.review_promotion(args.task_id, run_id=args.run_id, create_follow_on=not args.no_follow_on)
        emit({"promotion": serialize_dataclass(result.promotion), "follow_on_task_id": result.follow_on_task_id})
        return True
    if args.command == "affirm-promotion":
        ensure_llm_ready(args, ctx, reason="Promotion affirmation")
        result = engine.affirm_promotion(
            args.task_id,
            run_id=args.run_id,
            promotion_id=args.promotion_id,
            create_follow_on=not args.no_follow_on,
        )
        emit({"promotion": serialize_dataclass(result.promotion), "follow_on_task_id": result.follow_on_task_id})
        return True
    if args.command == "rereview-promotion":
        result = engine.rereview_promotion(
            args.task_id,
            remediation_task_id=args.remediation_task_id,
            remediation_run_id=args.remediation_run_id,
            base_promotion_id=args.base_promotion_id,
            create_follow_on=not args.no_follow_on,
        )
        emit({"promotion": serialize_dataclass(result.promotion), "follow_on_task_id": result.follow_on_task_id})
        return True
    if args.command == "chaos":
        from ..chaos.runner import ChaosRunner, write_chaos_report
        from ..chaos.heartbeat import drain_chaos_findings
        from ..chaos.injectors import ALL_INJECTORS, ShadowSupervisorInjector

        # Configure shadow supervisor from CLI args
        injectors = []
        for inj in ALL_INJECTORS:
            if isinstance(inj, ShadowSupervisorInjector):
                heartbeat_pids = [args.project_id] if args.project_id and args.shadow_heartbeat_interval else None
                injectors.append(ShadowSupervisorInjector(
                    max_iterations=args.shadow_iterations,
                    heartbeat_project_ids=heartbeat_pids,
                    heartbeat_interval_seconds=args.shadow_heartbeat_interval,
                ))
            else:
                injectors.append(inj)

        runner = ChaosRunner(
            config=config,
            injectors=injectors,
            memory_limit_mb=args.memory_limit_mb,
            cpu_limit_seconds=args.cpu_limit_seconds,
            feed_to_project_id=args.project_id or "",
        )
        drain_payload: dict[str, object] | None = None
        if args.dry_run or not args.project_id:
            chaos_round = runner.run()
        else:
            chaos_round = runner.run_and_feed(store)
            drain_result = drain_chaos_findings(
                store=store,
                project_id=args.project_id,
                task_service=engine,
                min_severity=args.min_severity,
            )
            drain_payload = {
                "created_tasks": len(drain_result.created_tasks),
                "skipped_duplicates": drain_result.skipped_duplicates,
                "total_findings": drain_result.total_findings,
            }
        if args.report_path:
            write_chaos_report(chaos_round, Path(args.report_path))
        result = chaos_round.summary()
        if drain_payload is not None:
            result["drain"] = drain_payload
        emit(result)
        return True
    if args.command == "smoke-test":
        project = _get_or_create_smoke_project(engine, store, args.project_name)
        task = engine.create_task_with_policy(
            project_id=project.id, title=args.task_title, objective=args.objective,
            priority=100, parent_task_id=None, source_run_id=None,
            external_ref_type=None, external_ref_id=None,
            validation_profile="generic", strategy="smoke", max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        runs = engine.run_until_stable(task.id)
        payload = {
            "project": serialize_dataclass(project),
            "task": serialize_dataclass(store.get_task(task.id)),
            "runs": [serialize_dataclass(r) for r in runs],
            "events": [serialize_dataclass(i) for i in store.list_events("task", task.id)],
        }
        if args.json:
            emit(payload)
        else:
            print(_smoke_test_text(payload))
        return True
    return False
