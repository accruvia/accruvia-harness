from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass
import sys
from typing import Any

from ..bootstrap import build_engine_from_config, build_store, build_telemetry
from ..control_breadcrumbs import BreadcrumbWriter
from ..control_classifier import FailureClassifier
from ..config import HarnessConfig, default_config_path, write_persisted_config
from ..control_plane import ControlPlane
from ..control_runtime import ControlRuntimeObserver
from ..control_watch import ControlWatchService
from ..domain import serialize_dataclass
from ..engine import HarnessEngine
from ..github import GitHubCLI
from ..gitlab import GitLabCLI
from ..interrogation import HarnessQueryService, InterrogationService
from ..llm import build_llm_router
from ..onboarding import detect_llm_command_candidates, probe_llm_command, prompt_text
from ..runtime import WorkflowRuntime, build_runtime
from ..store import SQLiteHarnessStore
from ..telemetry import TelemetrySink


_OUTPUT_JSON = False
_AUTO_CONFIGURE_PROBE_TIMEOUT_SECONDS = 5


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


def build_context(config: HarnessConfig) -> CLIContext:
    store = build_store(config)
    telemetry = build_telemetry(config)
    control_plane = ControlPlane(store)
    failure_classifier = FailureClassifier()
    breadcrumb_writer = BreadcrumbWriter(store, config.workspace_root)
    engine = build_engine_from_config(config, store=store, telemetry=telemetry)
    query_service = HarnessQueryService(store, telemetry=telemetry)
    return CLIContext(
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
        ),
        control_runtime=ControlRuntimeObserver(
            store,
            control_plane,
            failure_classifier,
            breadcrumb_writer,
        ),
    )


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
    return resolved
