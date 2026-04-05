from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

from ..bootstrap import build_engine_from_config, build_store, build_telemetry
from ..control_breadcrumbs import BreadcrumbWriter
from ..control_classifier import FailureClassifier
from ..config import HarnessConfig, default_config_path, write_persisted_config
from ..control_plane import ControlPlane
from ..control_runtime import ControlRuntimeObserver
from ..control_watch import ControlWatchService
from ..sa_watch import SAWatchService
from ..domain import ControlRecoveryAction, new_id, serialize_dataclass
from ..engine import HarnessEngine
from ..github import GitHubCLI
from ..gitlab import GitLabCLI
from ..interrogation import HarnessQueryService, InterrogationService
from ..llm import build_llm_router
from ..onboarding import detect_llm_command_candidates, probe_llm_command, prompt_text
from ..runtime import WorkflowRuntime, build_runtime
from ..services.workflow_service import WorkflowService
from ..store import SQLiteHarnessStore
from ..telemetry import TelemetrySink


_OUTPUT_JSON = False
_AUTO_CONFIGURE_PROBE_TIMEOUT_SECONDS = 5
_UI_RUNTIME_STATE_FILENAME = "ui_runtime_state.json"
_UI_DESIRED_STATE_FILENAME = "desired_ui.json"
_SUPERVISOR_DESIRED_STATE_FILENAME = "desired_supervisor.json"
_STACK_RESTART_REQUEST_FILENAME = "restart_stack_request.json"
_SA_WATCH_RUNTIME_STATE_FILENAME = "sa_watch_runtime_state.json"
_SA_WATCH_DESIRED_STATE_FILENAME = "desired_sa_watch.json"
_SA_WATCH_LAUNCH_STATE_FILENAME = "sa_watch_launch_state.json"
_SA_WATCH_LAUNCH_STALE_SECONDS = 30.0


def set_output_mode(*, json_enabled: bool) -> None:
    global _OUTPUT_JSON
    _OUTPUT_JSON = json_enabled


def _format_scalar(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _render_text(value: Any, *, indent: int = 0, label: str | None = None) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{prefix}{label}: none"] if label is not None else [f"{prefix}none"]
        lines: list[str] = []
        if label is not None:
            lines.append(f"{prefix}{label}:")
        child_indent = indent + (2 if label is not None else 0)
        for key, child in value.items():
            lines.extend(_render_text(child, indent=child_indent, label=key.replace("_", " ")))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix}{label}: none"] if label is not None else [f"{prefix}none"]
        lines: list[str] = []
        if label is not None:
            lines.append(f"{prefix}{label}:")
            child_indent = indent + 2
        else:
            child_indent = indent
        for item in value:
            bullet_prefix = " " * child_indent
            if isinstance(item, (dict, list)):
                lines.append(f"{bullet_prefix}-")
                lines.extend(_render_text(item, indent=child_indent + 2))
            else:
                lines.append(f"{bullet_prefix}- {_format_scalar(item)}")
        return lines
    if label is None:
        return [f"{prefix}{_format_scalar(value)}"]
    return [f"{prefix}{label}: {_format_scalar(value)}"]


def emit(payload: Any) -> None:
    if _OUTPUT_JSON:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print("\n".join(_render_text(payload)))


def _recovery_status_line(text: str) -> str:
    return f"{datetime.now().astimezone().strftime('%H:%M:%S')} recovery {text}"


@dataclass(slots=True)
class CLIContext:
    config: HarnessConfig
    store: SQLiteHarnessStore
    engine: HarnessEngine
    github: GitHubCLI
    gitlab: GitLabCLI
    runtime: WorkflowRuntime
    query_service: HarnessQueryService
    interrogation_service: InterrogationService
    telemetry: TelemetrySink
    control_plane: ControlPlane
    failure_classifier: FailureClassifier
    breadcrumb_writer: BreadcrumbWriter
    control_watch: ControlWatchService
    control_runtime: ControlRuntimeObserver
    sa_watch: SAWatchService
    workflow_data_service: Any | None = None


def _control_plane_runtime_dir(config: HarnessConfig) -> Path:
    path = config.db_path.parent / "control"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def stack_restart_request_path(config: HarnessConfig) -> Path:
    return _control_plane_runtime_dir(config) / _STACK_RESTART_REQUEST_FILENAME


def record_stack_restart_request(config: HarnessConfig, payload: dict[str, Any]) -> Path:
    request = dict(payload)
    request.setdefault("requested_at", time.time())
    path = stack_restart_request_path(config)
    _write_json_file(path, request)
    return path


def read_stack_restart_request(config: HarnessConfig) -> dict[str, Any] | None:
    return _read_json_file(stack_restart_request_path(config))


def clear_stack_restart_request(config: HarnessConfig) -> None:
    try:
        stack_restart_request_path(config).unlink()
    except FileNotFoundError:
        pass


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def ui_runtime_state_path(config: HarnessConfig) -> Path:
    return _control_plane_runtime_dir(config) / _UI_RUNTIME_STATE_FILENAME


def desired_ui_state_path(config: HarnessConfig) -> Path:
    return _control_plane_runtime_dir(config) / _UI_DESIRED_STATE_FILENAME


def desired_supervisor_state_path(config: HarnessConfig) -> Path:
    return _control_plane_runtime_dir(config) / _SUPERVISOR_DESIRED_STATE_FILENAME


def sa_watch_runtime_state_path(config: HarnessConfig) -> Path:
    return _control_plane_runtime_dir(config) / _SA_WATCH_RUNTIME_STATE_FILENAME


def desired_sa_watch_state_path(config: HarnessConfig) -> Path:
    return _control_plane_runtime_dir(config) / _SA_WATCH_DESIRED_STATE_FILENAME


def sa_watch_launch_state_path(config: HarnessConfig) -> Path:
    return _control_plane_runtime_dir(config) / _SA_WATCH_LAUNCH_STATE_FILENAME


def record_desired_ui_state(
    config: HarnessConfig,
    *,
    host: str,
    port: int,
    open_browser: bool,
    project_ref: str | None,
) -> None:
    _write_json_file(
        desired_ui_state_path(config),
        {
            "host": host,
            "port": port,
            "open_browser": open_browser,
            "project_ref": project_ref,
        },
    )


def update_ui_runtime_state(
    config: HarnessConfig,
    *,
    host: str,
    preferred_port: int,
    resolved_port: int,
    project_ref: str | None,
) -> None:
    _write_json_file(
        ui_runtime_state_path(config),
        {
            "pid": os.getpid(),
            "host": host,
            "preferred_port": preferred_port,
            "resolved_port": resolved_port,
            "project_ref": project_ref,
        },
    )


def clear_ui_runtime_state(config: HarnessConfig) -> None:
    path = ui_runtime_state_path(config)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def stop_ui_process(config: HarnessConfig) -> dict[str, Any]:
    runtime_state = _read_json_file(ui_runtime_state_path(config)) or {}
    pid = int(runtime_state.get("pid") or 0)
    if pid > 0:
        _terminate_pid(pid)
    clear_ui_runtime_state(config)
    return {"stopped": pid > 0, "pid": pid if pid > 0 else None}


def update_sa_watch_runtime_state(
    config: HarnessConfig,
    *,
    interval_seconds: float,
    mode: str,
    last_decision: str | None = None,
    last_reason: str | None = None,
) -> None:
    _write_json_file(
        sa_watch_runtime_state_path(config),
        {
            "pid": os.getpid(),
            "interval_seconds": interval_seconds,
            "mode": mode,
            "last_decision": last_decision,
            "last_reason": last_reason,
            "heartbeat_at": time.time(),
        },
    )


def clear_sa_watch_runtime_state(config: HarnessConfig) -> None:
    path = sa_watch_runtime_state_path(config)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def clear_sa_watch_launch_state(config: HarnessConfig) -> None:
    path = sa_watch_launch_state_path(config)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def record_desired_sa_watch_state(config: HarnessConfig, *, interval_seconds: float) -> None:
    _write_json_file(
        desired_sa_watch_state_path(config),
        {
            "interval_seconds": interval_seconds,
            "updated_at": time.time(),
        },
    )


def read_desired_sa_watch_state(config: HarnessConfig) -> dict[str, Any] | None:
    return _read_json_file(desired_sa_watch_state_path(config))


def clear_desired_sa_watch_state(config: HarnessConfig) -> None:
    path = desired_sa_watch_state_path(config)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def read_sa_watch_runtime_state(config: HarnessConfig) -> dict[str, Any] | None:
    return _read_json_file(sa_watch_runtime_state_path(config))


def read_sa_watch_launch_state(config: HarnessConfig) -> dict[str, Any] | None:
    return _read_json_file(sa_watch_launch_state_path(config))


def _sa_watch_launch_is_active(payload: dict[str, Any] | None, *, stale_after_seconds: float) -> bool:
    if not isinstance(payload, dict):
        return False
    created_at = float(payload.get("created_at") or 0.0)
    age_seconds = max(time.time() - created_at, 0.0) if created_at > 0 else stale_after_seconds + 1.0
    child_pid = int(payload.get("pid") or 0)
    launcher_pid = int(payload.get("launcher_pid") or 0)
    if child_pid > 0 and _pid_is_alive(child_pid):
        return True
    if launcher_pid > 0 and _pid_is_alive(launcher_pid) and age_seconds <= stale_after_seconds:
        return True
    return False


def _acquire_sa_watch_launch_state(config: HarnessConfig, *, interval_seconds: float) -> dict[str, Any] | None:
    path = sa_watch_launch_state_path(config)
    payload = {
        "launcher_pid": os.getpid(),
        "pid": 0,
        "interval_seconds": interval_seconds,
        "created_at": time.time(),
    }
    for _ in range(2):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = read_sa_watch_launch_state(config)
            if _sa_watch_launch_is_active(existing, stale_after_seconds=_SA_WATCH_LAUNCH_STALE_SECONDS):
                return None
            clear_sa_watch_launch_state(config)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True))
        return payload
    return None


def record_desired_supervisor_state(
    config: HarnessConfig,
    *,
    project_id: str | None,
    worker_id: str,
    watch: bool,
    lease_seconds: int,
    idle_sleep_seconds: float,
    max_idle_cycles: int | None,
    max_iterations: int | None,
    heartbeat_project_ids: list[str],
    heartbeat_interval_seconds: float | None,
    heartbeat_all_projects: bool,
    review_check_enabled: bool,
    review_check_interval_seconds: int | None,
) -> None:
    _write_json_file(
        desired_supervisor_state_path(config),
        {
            "project_id": project_id,
            "worker_id": worker_id,
            "watch": watch,
            "lease_seconds": lease_seconds,
            "idle_sleep_seconds": idle_sleep_seconds,
            "max_idle_cycles": max_idle_cycles,
            "max_iterations": max_iterations,
            "heartbeat_project_ids": heartbeat_project_ids,
            "heartbeat_interval_seconds": heartbeat_interval_seconds,
            "heartbeat_all_projects": heartbeat_all_projects,
            "review_check_enabled": review_check_enabled,
            "review_check_interval_seconds": review_check_interval_seconds,
        },
    )


def desired_api_url(config: HarnessConfig) -> str:
    runtime_state = _read_json_file(ui_runtime_state_path(config)) or {}
    desired_state = _read_json_file(desired_ui_state_path(config)) or {}
    host = str(runtime_state.get("host") or desired_state.get("host") or "127.0.0.1")
    port = int(runtime_state.get("resolved_port") or desired_state.get("port") or 9100)
    return f"http://{host}:{port}/api/version"


def _terminate_pid(pid: int, *, timeout_seconds: float = 5.0) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_cwd(pid: int) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
    except OSError:
        return None


def _matching_sa_watch_pids(config: HarnessConfig) -> list[int]:
    try:
        output = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True)
    except (OSError, subprocess.SubprocessError):
        return []
    repo_cwd = Path.cwd().resolve()
    target_db = config.db_path.resolve()
    target_workspace = config.workspace_root.resolve()
    matches: list[int] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_text, _, args = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid <= 0 or pid == os.getpid() or not _pid_is_alive(pid):
            continue
        try:
            argv = shlex.split(args)
        except ValueError:
            continue
        if "sa-watch-loop" not in argv:
            continue
        matched = False
        if "--db" in argv:
            try:
                db_path = Path(argv[argv.index("--db") + 1]).resolve()
            except (IndexError, OSError):
                db_path = None
            matched = db_path == target_db
        elif "--workspace" in argv:
            try:
                workspace_path = Path(argv[argv.index("--workspace") + 1]).resolve()
            except (IndexError, OSError):
                workspace_path = None
            matched = workspace_path == target_workspace
        else:
            matched = _process_cwd(pid) == repo_cwd
        if matched:
            matches.append(pid)
    return sorted(dict.fromkeys(matches))


def _reconcile_matching_sa_watch_processes(config: HarnessConfig) -> tuple[int | None, list[int]]:
    matches = _matching_sa_watch_pids(config)
    if not matches:
        return None, []
    survivor = max(matches)
    extras = [pid for pid in matches if pid != survivor]
    for pid in extras:
        _terminate_pid(pid)
    return survivor, extras


def restart_api_process(config: HarnessConfig, *, force: bool = False) -> dict[str, Any] | None:
    desired = _read_json_file(desired_ui_state_path(config))
    if desired is None:
        return None
    runtime_state = _read_json_file(ui_runtime_state_path(config)) or {}
    pid = int(runtime_state.get("pid") or 0)
    if _pid_is_alive(pid) and not force:
        # If the recorded UI process is already alive, let the watch loop wait
        # for that process to finish binding the expected endpoint before we
        # spawn a second server that can drift onto an alternate port.
        return {"pid": pid, "existing": True}
    if pid > 0:
        _terminate_pid(pid)
    host = str(desired.get("host") or "127.0.0.1")
    port = int(runtime_state.get("resolved_port") or desired.get("port") or 9100)
    command = [sys.executable, "-m", "accruvia_harness", "ui", "--host", host, "--port", str(port), "--no-open-browser"]
    project_ref = desired.get("project_ref")
    if project_ref:
        command.extend(["--project-id", str(project_ref)])
    log_path = _control_plane_runtime_dir(config) / "restart_ui.log"
    with log_path.open("ab") as handle:
        process = subprocess.Popen(
            command,
            cwd=str(Path.cwd()),
            env=os.environ.copy(),
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    return {"pid": process.pid, "command": command, "log_path": str(log_path)}


def build_supervise_restart_command(record: dict[str, Any]) -> list[str]:
    command = [sys.executable, "-m", "accruvia_harness", "run-harness"]
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


def restart_harness_process(config: HarnessConfig, *, force: bool = False) -> dict[str, Any] | None:
    desired = _read_json_file(desired_supervisor_state_path(config))
    if desired is None:
        return None
    if force:
        control_dir = config.db_path.parent / "supervisors"
        for record in list_desired_supervisors(config):
            pid = int(record.get("pid") or 0)
            if pid > 0:
                _terminate_pid(pid)
    command = build_supervise_restart_command(desired)
    control_dir = config.db_path.parent / "supervisors"
    control_dir.mkdir(parents=True, exist_ok=True)
    stop_request_path = control_dir / "stop.request"
    # A killed supervisor leaves stop.request behind via its signal handler.
    # Clear it before relaunching or the replacement supervisor exits immediately.
    try:
        stop_request_path.unlink()
    except FileNotFoundError:
        pass
    worker_id = str(desired.get("worker_id") or "supervisor")
    project_id = str(desired.get("project_id") or "all-projects")
    restart_log_path = control_dir / f"restart_{project_id}_{worker_id}.log"
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
        "project_id": desired.get("project_id"),
        "worker_id": desired.get("worker_id"),
        "command": command,
        "restart_log_path": str(restart_log_path),
    }


def build_control_loop_restart_command(args) -> list[str]:
    command = [sys.executable, "-m", "accruvia_harness.cli", "control-loop", "--interval-seconds", str(args.interval_seconds)]
    if getattr(args, "api_url", None):
        command.extend(["--api-url", str(args.api_url)])
    if getattr(args, "stalled_objective_hours", None) is not None:
        command.extend(["--stalled-objective-hours", str(args.stalled_objective_hours)])
    if bool(getattr(args, "no_freeze_on_stall", False)):
        command.append("--no-freeze-on-stall")
    if getattr(args, "max_iterations", None) is not None:
        command.extend(["--max-iterations", str(args.max_iterations)])
    return command


def restart_control_loop_process(config: HarnessConfig, args) -> dict[str, Any]:
    command = build_control_loop_restart_command(args)
    log_path = _control_plane_runtime_dir(config) / "restart_control_loop.log"
    with log_path.open("ab") as handle:
        process = subprocess.Popen(
            command,
            cwd=str(Path.cwd()),
            env=os.environ.copy(),
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    return {"pid": process.pid, "command": command, "log_path": str(log_path)}


def build_sa_watch_restart_command(config: HarnessConfig, *, interval_seconds: float) -> list[str]:
    return [
        sys.executable,
        "-m",
        "accruvia_harness.cli",
        "--db",
        str(config.db_path),
        "--workspace",
        str(config.workspace_root),
        "--log-path",
        str(config.log_path),
        "--config-file",
        str(default_config_path(config.db_path.parent)),
        "sa-watch-loop",
        "--interval-seconds",
        str(interval_seconds),
    ]


def start_sa_watch_process(
    config: HarnessConfig,
    *,
    interval_seconds: float,
    force: bool = False,
    stream_output: bool = False,
) -> dict[str, Any]:
    if force:
        survivor_pid = None
        extra_pids = _matching_sa_watch_pids(config)
        for orphan_pid in extra_pids:
            _terminate_pid(orphan_pid)
    else:
        survivor_pid, extra_pids = _reconcile_matching_sa_watch_processes(config)
        if survivor_pid is not None:
            update_sa_watch_runtime_state(
                config,
                interval_seconds=interval_seconds,
                mode="adopted",
                last_reason="reconciled_existing_process",
            )
            clear_sa_watch_launch_state(config)
            return {
                "pid": survivor_pid,
                "existing": True,
                "adopted_existing": True,
                "reconciled_orphan_pids": extra_pids,
                "interval_seconds": interval_seconds,
                "stream_output": stream_output,
                "log_path": str(_control_plane_runtime_dir(config) / "sa_watch.log"),
            }
    runtime_state = read_sa_watch_runtime_state(config) or {}
    pid = int(runtime_state.get("pid") or 0)
    log_path = _control_plane_runtime_dir(config) / "sa_watch.log"
    if _pid_is_alive(pid) and not force:
        return {
            "pid": pid,
            "existing": True,
            "interval_seconds": float(runtime_state.get("interval_seconds") or interval_seconds),
            "stream_output": stream_output,
            "log_path": str(log_path),
        }
    launch_state = read_sa_watch_launch_state(config)
    if _sa_watch_launch_is_active(launch_state, stale_after_seconds=_SA_WATCH_LAUNCH_STALE_SECONDS) and not force:
        return {
            "pid": int((launch_state or {}).get("pid") or 0),
            "existing_launch": True,
            "interval_seconds": float((launch_state or {}).get("interval_seconds") or interval_seconds),
            "stream_output": stream_output,
            "log_path": str(log_path),
        }
    if pid > 0:
        _terminate_pid(pid)
    if force:
        clear_sa_watch_launch_state(config)
    launch_payload = _acquire_sa_watch_launch_state(config, interval_seconds=interval_seconds)
    if launch_payload is None and not force:
        refreshed = read_sa_watch_launch_state(config) or {}
        return {
            "pid": int(refreshed.get("pid") or 0),
            "existing_launch": True,
            "interval_seconds": float(refreshed.get("interval_seconds") or interval_seconds),
            "stream_output": stream_output,
            "log_path": str(log_path),
        }
    record_desired_sa_watch_state(config, interval_seconds=interval_seconds)
    command = build_sa_watch_restart_command(config, interval_seconds=interval_seconds)
    if stream_output:
            process = subprocess.Popen(
                command,
                cwd=str(Path.cwd()),
                env=os.environ.copy(),
                start_new_session=True,
            )
    else:
        with log_path.open("ab") as handle:
            process = subprocess.Popen(
                command,
                cwd=str(Path.cwd()),
                env=os.environ.copy(),
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
    _write_json_file(
        sa_watch_launch_state_path(config),
        {
            "launcher_pid": int((launch_payload or {}).get("launcher_pid") or os.getpid()),
            "pid": process.pid,
            "interval_seconds": interval_seconds,
            "created_at": float((launch_payload or {}).get("created_at") or time.time()),
        },
    )
    return {
        "pid": process.pid,
        "command": command,
        "log_path": str(log_path),
        "interval_seconds": interval_seconds,
        "stream_output": stream_output,
    }


def stop_sa_watch_process(config: HarnessConfig) -> dict[str, Any]:
    runtime_state = read_sa_watch_runtime_state(config) or {}
    pid = int(runtime_state.get("pid") or 0)
    launch_state = read_sa_watch_launch_state(config) or {}
    launch_pid = int(launch_state.get("pid") or 0)
    matched_pids = _matching_sa_watch_pids(config)
    if pid > 0:
        _terminate_pid(pid)
    elif launch_pid > 0:
        _terminate_pid(launch_pid)
    for orphan_pid in matched_pids:
        if orphan_pid not in {pid, launch_pid}:
            _terminate_pid(orphan_pid)
    clear_sa_watch_runtime_state(config)
    clear_sa_watch_launch_state(config)
    clear_desired_sa_watch_state(config)
    effective_pid = pid if pid > 0 else launch_pid
    return {
        "stopped": effective_pid > 0 or bool(matched_pids),
        "pid": effective_pid if effective_pid > 0 else None,
        "reconciled_orphan_pids": [orphan_pid for orphan_pid in matched_pids if orphan_pid not in {pid, launch_pid}],
    }


def list_desired_supervisors(config: HarnessConfig) -> list[dict[str, Any]]:
    control_dir = config.db_path.parent / "supervisors"
    if not control_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(control_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("pid") is not None:
            records.append(record)
    return records


def startup_preflight(config: HarnessConfig, store: SQLiteHarnessStore) -> dict[str, Any]:
    control_dir = config.db_path.parent / "supervisors"
    control_dir.mkdir(parents=True, exist_ok=True)
    recovered = store.recover_stale_state()
    stale_supervisor_records: list[int] = []
    for record in list_desired_supervisors(config):
        pid = int(record.get("pid") or 0)
        if pid > 0 and _pid_is_alive(pid):
            continue
        stale_supervisor_records.append(pid)
        path = control_dir / f"{pid}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    stop_request_cleared = False
    stop_request_path = control_dir / "stop.request"
    if stop_request_path.exists() and not list_desired_supervisors(config):
        stop_request_path.unlink()
        stop_request_cleared = True

    stale_ui_runtime = False
    ui_runtime = _read_json_file(ui_runtime_state_path(config)) or {}
    ui_pid = int(ui_runtime.get("pid") or 0)
    if ui_pid > 0 and not _pid_is_alive(ui_pid):
        clear_ui_runtime_state(config)
        stale_ui_runtime = True

    stale_sa_watch_runtime = False
    sa_watch_runtime = read_sa_watch_runtime_state(config) or {}
    sa_watch_pid = int(sa_watch_runtime.get("pid") or 0)
    if sa_watch_pid > 0 and not _pid_is_alive(sa_watch_pid):
        clear_sa_watch_runtime_state(config)
        stale_sa_watch_runtime = True

    stale_sa_watch_launch = False
    sa_watch_launch = read_sa_watch_launch_state(config)
    if sa_watch_launch is not None and not _sa_watch_launch_is_active(
        sa_watch_launch,
        stale_after_seconds=_SA_WATCH_LAUNCH_STALE_SECONDS,
    ):
        clear_sa_watch_launch_state(config)
        stale_sa_watch_launch = True

    stale_restart_request = read_stack_restart_request(config)
    restart_request_cleared = False
    if stale_restart_request is not None:
        clear_stack_restart_request(config)
        restart_request_cleared = True

    stuck_snapshots_cleared = 0
    with store.connect() as conn:
        result = conn.execute("DELETE FROM control_events WHERE event_type = 'stuck_snapshot'")
        stuck_snapshots_cleared = result.rowcount

    return {
        "recovered": recovered,
        "stale_supervisor_records": stale_supervisor_records,
        "stop_request_cleared": stop_request_cleared,
        "stale_ui_runtime_cleared": stale_ui_runtime,
        "stale_sa_watch_runtime_cleared": stale_sa_watch_runtime,
        "stale_sa_watch_launch_cleared": stale_sa_watch_launch,
        "stale_restart_request_cleared": restart_request_cleared,
        "stuck_snapshots_cleared": stuck_snapshots_cleared,
    }


def build_context(config: HarnessConfig) -> CLIContext:
    store = build_store(config)
    telemetry = build_telemetry(config)
    control_plane = ControlPlane(store)
    breadcrumb_writer = BreadcrumbWriter(store, config.workspace_root)
    engine = build_engine_from_config(config, store=store, telemetry=telemetry)
    failure_classifier = FailureClassifier(
        llm_router=engine.llm_router,
        workspace_root=config.workspace_root,
        telemetry=telemetry,
    )
    query_service = HarnessQueryService(store, telemetry=telemetry)
    ctx = CLIContext(
        config=config,
        store=store,
        engine=engine,
        github=GitHubCLI(),
        gitlab=GitLabCLI(),
        runtime=build_runtime(
            backend=config.runtime_backend,
            config=config,
            engine=engine,
            temporal_target=config.temporal_target,
            temporal_namespace=config.temporal_namespace,
            temporal_task_queue=config.temporal_task_queue,
        ),
        query_service=query_service,
        interrogation_service=InterrogationService(
            query_service=query_service,
            workspace_root=config.workspace_root,
            llm_router=engine.llm_router,
            telemetry=telemetry,
        ),
        telemetry=telemetry,
        control_plane=control_plane,
        failure_classifier=failure_classifier,
        breadcrumb_writer=breadcrumb_writer,
        control_watch=ControlWatchService(
            store,
            control_plane,
            failure_classifier,
            breadcrumb_writer,
            supervisor_control_dir=config.db_path.parent / "supervisors",
            restart_api=lambda: restart_api_process(config),
            restart_harness=lambda: restart_harness_process(config),
        ),
        control_runtime=ControlRuntimeObserver(
            store,
            control_plane,
            failure_classifier,
            breadcrumb_writer,
        ),
        sa_watch=SAWatchService(
            store,
            control_plane,
            engine.llm_router,
            config.workspace_root,
            engine=engine,
        ),
    )
    if hasattr(ctx.engine, "queue"):
        # The harness must reconcile objective workflow after every task finish
        # regardless of whether work is driven by the UI or the supervisor CLI.
        # If only the UI wires this callback, successful structural repairs can
        # complete and still leave the objective paused with no new runnable work.
        try:
            from ..ui import HarnessUIDataService
        except ModuleNotFoundError as exc:
            if exc.name != "fastapi":
                raise
            HarnessUIDataService = None

        if HarnessUIDataService is not None:
            data_service = HarnessUIDataService(ctx)
            ctx.workflow_data_service = data_service
            ctx.engine.queue.post_task_callback = data_service.reconcile_task_workflow
            ctx.sa_watch.post_repair_callback = data_service.reconcile_task_workflow
        else:
            workflow_service = WorkflowService(store)
            ctx.workflow_data_service = workflow_service

            def _reconcile_task_workflow(task) -> None:
                objective_id = str(getattr(task, "objective_id", "") or "").strip()
                if objective_id:
                    workflow_service.reconcile_objective(objective_id)

            ctx.engine.queue.post_task_callback = _reconcile_task_workflow
            ctx.sa_watch.post_repair_callback = _reconcile_task_workflow
        ctx.sa_watch.structural_progress_callback = ctx.control_runtime.handle
        ctx.sa_watch.restart_stack = lambda payload: _restart_stack_from_sa_watch(ctx, payload)
    return ctx


def _resolved_config_file(args, config: HarnessConfig) -> Path:
    if getattr(args, "config_file", None):
        return Path(args.config_file)
    return default_config_path(config.db_path.parent)


def resolve_project_ref(ctx: CLIContext, ref: str | None) -> str | None:
    if ref is None:
        return None
    project = ctx.store.get_project(ref)
    if project is not None:
        return project.id
    if ref.startswith("project_"):
        return ref
    matches = [project for project in ctx.store.list_projects() if project.name == ref]
    if len(matches) == 1:
        return matches[0].id
    if len(matches) > 1:
        ids = ", ".join(project.id for project in matches)
        raise ValueError(f"Project name '{ref}' is ambiguous. Matching ids: {ids}")
    raise ValueError(f"Unknown project: {ref}")


def resolve_project_args(args, ctx: CLIContext) -> None:
    if hasattr(args, "project_id") and getattr(args, "project_id") is not None:
        args.project_id = resolve_project_ref(ctx, getattr(args, "project_id"))
    if hasattr(args, "heartbeat_project_ids") and getattr(args, "heartbeat_project_ids", None):
        args.heartbeat_project_ids = [
            resolve_project_ref(ctx, project_ref)
            for project_ref in list(getattr(args, "heartbeat_project_ids") or [])
        ]


def ensure_llm_ready(args, ctx: CLIContext, *, reason: str) -> HarnessConfig:
    config = ctx.config
    available = [item for item in detect_llm_command_candidates() if item.available]
    existing_updates: dict[str, object] = {}
    if config.llm_codex_command and any(item.backend == "codex" for item in available):
        detected_codex = next(item for item in available if item.backend == "codex")
        if config.llm_codex_command != detected_codex.command:
            existing_updates["llm_codex_command"] = detected_codex.command
    if config.llm_claude_command and any(item.backend == "claude" for item in available):
        detected_claude = next(item for item in available if item.backend == "claude")
        if config.llm_claude_command != detected_claude.command:
            existing_updates["llm_claude_command"] = detected_claude.command
    if existing_updates:
        config_path = _resolved_config_file(args, config)
        payload = config.persisted_payload()
        payload.update(existing_updates)
        write_persisted_config(config_path, payload)
        resolved = HarnessConfig.from_env(
            getattr(args, "db", None),
            getattr(args, "workspace", None),
            getattr(args, "log_path", None),
            getattr(args, "config_file", None),
        )
        ctx.config = resolved
        ctx.engine.set_llm_router(build_llm_router(resolved, telemetry=ctx.telemetry))
        ctx.interrogation_service.llm_router = ctx.engine.llm_router
        ctx.sa_watch.llm_router = ctx.engine.llm_router
        config = resolved
    if any(
        (
            config.llm_command,
            config.llm_codex_command,
            config.llm_claude_command,
            config.llm_accruvia_client_command,
        )
    ):
        return config
    if not available:
        raise ValueError(
            f"{reason} requires a configured LLM provider. Install Codex or Claude, or run `./bin/accruvia-harness configure-llm`."
        )
    if len(available) == 1:
        selected = available[0]
    else:
        if not sys.stdin.isatty():
            labels = ", ".join(item.label for item in available)
            raise ValueError(
                f"{reason} found multiple installed providers ({labels}). Run `./bin/accruvia-harness configure-llm` once to choose a default."
            )
        sys.stderr.write(f"{reason} found multiple installed providers.\n")
        for index, item in enumerate(available, start=1):
            sys.stderr.write(f"  {index}. {item.label}\n")
        choice = prompt_text("Choose the default provider to use", default="1")
        selected = available[int(choice) - 1]
    command_key = {
        "command": "llm_command",
        "codex": "llm_codex_command",
        "claude": "llm_claude_command",
        "accruvia_client": "llm_accruvia_client_command",
    }[selected.backend]
    probe = probe_llm_command(selected.command, timeout_seconds=_AUTO_CONFIGURE_PROBE_TIMEOUT_SECONDS)
    if not bool(probe.get("ok")):
        raise ValueError(
            f"{reason} found {selected.label} on PATH, but the detected command is not ready: {probe.get('message')}. "
            "Run `./bin/accruvia-harness configure-llm` once to choose or fix a working provider."
        )
    payload = config.persisted_payload()
    payload["llm_backend"] = selected.backend
    payload[command_key] = selected.command
    config_path = _resolved_config_file(args, config)
    write_persisted_config(config_path, payload)
    resolved = HarnessConfig.from_env(
        getattr(args, "db", None),
        getattr(args, "workspace", None),
        getattr(args, "log_path", None),
        getattr(args, "config_file", None),
    )
    ctx.config = resolved
    ctx.engine.set_llm_router(build_llm_router(resolved, telemetry=ctx.telemetry))
    ctx.interrogation_service.llm_router = ctx.engine.llm_router
    ctx.sa_watch.llm_router = ctx.engine.llm_router
    return resolved


def _restart_stack_from_sa_watch(ctx: CLIContext, payload: dict[str, object]) -> dict[str, object]:
    # sa-watch owns the architectural repair decision, but the reboot itself
    # must still happen from the outer control layer so the mutated services are
    # restarted onto the newly-written code. This helper keeps that restart in
    # process while preserving deterministic control-plane verification.
    ctx.store.create_control_recovery_action(
        ControlRecoveryAction(
            id=new_id("recovery"),
            action_type="restart",
            target_type="system",
            target_id="system",
            reason=str(payload.get("reason") or "sa_watch_requested"),
            result="applied",
        )
    )
    restart_api_process(ctx.config, force=True)
    restart_harness_process(ctx.config, force=True)
    return ctx.control_watch.run_once(api_url=desired_api_url(ctx.config))
