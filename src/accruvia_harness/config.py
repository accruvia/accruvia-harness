from __future__ import annotations

import logging
import os
from dataclasses import asdict
from dataclasses import dataclass, field
import json
from pathlib import Path

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r, using default %s", name, raw, default)
        return default


@dataclass(slots=True)
class HarnessConfig:
    db_path: Path
    workspace_root: Path
    log_path: Path
    default_project_name: str
    default_repo: str
    runtime_backend: str
    temporal_target: str
    temporal_namespace: str
    temporal_task_queue: str
    worker_backend: str
    worker_command: str | None
    llm_backend: str
    llm_model: str | None
    llm_command: str | None
    llm_codex_command: str | None
    llm_claude_command: str | None
    llm_accruvia_client_command: str | None
    env_passthrough: tuple[str, ...] = field(default_factory=tuple)
    adapter_modules: tuple[str, ...] = field(default_factory=tuple)
    project_adapter_modules: tuple[str, ...] = field(default_factory=tuple)
    validator_modules: tuple[str, ...] = field(default_factory=tuple)
    telemetry_dir: Path = Path(".accruvia-harness/telemetry")
    telemetry_fsync_writes: bool = False
    otel_service_name: str = "accruvia-harness"
    otel_exporter_otlp_endpoint: str | None = None
    issue_close_on_completed: bool = True
    issue_close_only_on_approved_promotion: bool = False
    issue_reopen_on_pending: bool = True
    issue_reopen_on_active: bool = True
    issue_reopen_on_failed: bool = True
    timeout_ema_alpha: float = 0.5
    timeout_min_seconds: int = 30
    timeout_max_seconds: int = 1800
    timeout_multiplier: float = 2.5
    memory_limit_mb: int = 1024
    cpu_time_limit_seconds: int = 300
    observer_webhook_url: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        for path_key in ("db_path", "workspace_root", "log_path", "telemetry_dir"):
            payload[path_key] = str(payload[path_key])
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), sort_keys=True)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "HarnessConfig":
        return cls(
            db_path=Path(str(payload["db_path"])),
            workspace_root=Path(str(payload["workspace_root"])),
            log_path=Path(str(payload["log_path"])),
            telemetry_dir=Path(str(payload["telemetry_dir"])),
            telemetry_fsync_writes=bool(payload.get("telemetry_fsync_writes", False)),
            default_project_name=str(payload["default_project_name"]),
            default_repo=str(payload["default_repo"]),
            runtime_backend=str(payload["runtime_backend"]),
            temporal_target=str(payload["temporal_target"]),
            temporal_namespace=str(payload["temporal_namespace"]),
            temporal_task_queue=str(payload["temporal_task_queue"]),
            worker_backend=str(payload["worker_backend"]),
            worker_command=(str(payload["worker_command"]) if payload["worker_command"] is not None else None),
            llm_backend=str(payload["llm_backend"]),
            llm_model=(str(payload["llm_model"]) if payload["llm_model"] is not None else None),
            llm_command=(str(payload["llm_command"]) if payload["llm_command"] is not None else None),
            llm_codex_command=(str(payload["llm_codex_command"]) if payload["llm_codex_command"] is not None else None),
            llm_claude_command=(str(payload["llm_claude_command"]) if payload["llm_claude_command"] is not None else None),
            llm_accruvia_client_command=(
                str(payload["llm_accruvia_client_command"])
                if payload["llm_accruvia_client_command"] is not None
                else None
            ),
            env_passthrough=tuple(str(item) for item in payload.get("env_passthrough", ())),
            adapter_modules=tuple(str(item) for item in payload.get("adapter_modules", ())),
            project_adapter_modules=tuple(str(item) for item in payload.get("project_adapter_modules", ())),
            validator_modules=tuple(str(item) for item in payload.get("validator_modules", ())),
            otel_service_name=str(payload.get("otel_service_name", "accruvia-harness")),
            otel_exporter_otlp_endpoint=(
                str(payload["otel_exporter_otlp_endpoint"])
                if payload.get("otel_exporter_otlp_endpoint") is not None
                else None
            ),
            issue_close_on_completed=bool(payload.get("issue_close_on_completed", True)),
            issue_close_only_on_approved_promotion=bool(
                payload.get("issue_close_only_on_approved_promotion", False)
            ),
            issue_reopen_on_pending=bool(payload.get("issue_reopen_on_pending", True)),
            issue_reopen_on_active=bool(payload.get("issue_reopen_on_active", True)),
            issue_reopen_on_failed=bool(payload.get("issue_reopen_on_failed", True)),
            timeout_ema_alpha=float(payload.get("timeout_ema_alpha", 0.5)),
            timeout_min_seconds=int(payload.get("timeout_min_seconds", 30)),
            timeout_max_seconds=int(payload.get("timeout_max_seconds", 1800)),
            timeout_multiplier=float(payload.get("timeout_multiplier", 2.5)),
            memory_limit_mb=int(payload.get("memory_limit_mb", 1024)),
            cpu_time_limit_seconds=int(payload.get("cpu_time_limit_seconds", 300)),
            observer_webhook_url=(
                str(payload["observer_webhook_url"])
                if payload.get("observer_webhook_url") is not None
                else None
            ),
        )

    @classmethod
    def from_json(cls, payload: str) -> "HarnessConfig":
        return cls.from_payload(json.loads(payload))

    @classmethod
    def from_env(
        cls,
        db_path: str | Path | None = None,
        workspace_root: str | Path | None = None,
        log_path: str | Path | None = None,
    ) -> "HarnessConfig":
        base = Path(os.environ.get("ACCRUVIA_HARNESS_HOME", ".accruvia-harness"))
        resolved_db = Path(db_path) if db_path is not None else base / "harness.db"
        resolved_workspace = (
            Path(workspace_root) if workspace_root is not None else base / "workspace"
        )
        resolved_log = Path(log_path) if log_path is not None else base / "harness.log"
        resolved_telemetry = base / "telemetry"
        return cls(
            db_path=resolved_db,
            workspace_root=resolved_workspace,
            log_path=resolved_log,
            telemetry_dir=resolved_telemetry,
            telemetry_fsync_writes=os.environ.get("ACCRUVIA_TELEMETRY_FSYNC_WRITES", "false").lower() == "true",
            otel_service_name=os.environ.get("ACCRUVIA_OTEL_SERVICE_NAME", "accruvia-harness"),
            otel_exporter_otlp_endpoint=os.environ.get("ACCRUVIA_OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            default_project_name=os.environ.get("ACCRUVIA_HARNESS_PROJECT", "accruvia"),
            default_repo=os.environ.get("ACCRUVIA_HARNESS_REPO", "soverton/accruvia"),
            runtime_backend=os.environ.get("ACCRUVIA_HARNESS_RUNTIME", "local"),
            temporal_target=os.environ.get("ACCRUVIA_TEMPORAL_TARGET", "localhost:7233"),
            temporal_namespace=os.environ.get("ACCRUVIA_TEMPORAL_NAMESPACE", "default"),
            temporal_task_queue=os.environ.get("ACCRUVIA_TEMPORAL_TASK_QUEUE", "accruvia-harness"),
            worker_backend=os.environ.get("ACCRUVIA_WORKER_BACKEND", "local"),
            worker_command=os.environ.get("ACCRUVIA_WORKER_COMMAND"),
            llm_backend=os.environ.get("ACCRUVIA_LLM_BACKEND", "auto"),
            llm_model=os.environ.get("ACCRUVIA_LLM_MODEL"),
            llm_command=os.environ.get("ACCRUVIA_LLM_COMMAND"),
            llm_codex_command=os.environ.get("ACCRUVIA_LLM_CODEX_COMMAND"),
            llm_claude_command=os.environ.get("ACCRUVIA_LLM_CLAUDE_COMMAND"),
            llm_accruvia_client_command=os.environ.get("ACCRUVIA_LLM_ACCRUVIA_CLIENT_COMMAND"),
            env_passthrough=tuple(
                item.strip()
                for item in os.environ.get("ACCRUVIA_ENV_PASSTHROUGH", "").split(",")
                if item.strip()
            ),
            adapter_modules=tuple(
                item.strip()
                for item in os.environ.get("ACCRUVIA_ADAPTER_MODULES", "").split(",")
                if item.strip()
            ),
            project_adapter_modules=tuple(
                item.strip()
                for item in os.environ.get("ACCRUVIA_PROJECT_ADAPTER_MODULES", "").split(",")
                if item.strip()
            ),
            validator_modules=tuple(
                item.strip()
                for item in os.environ.get("ACCRUVIA_VALIDATOR_MODULES", "").split(",")
                if item.strip()
            ),
            issue_close_on_completed=os.environ.get("ACCRUVIA_ISSUE_CLOSE_ON_COMPLETED", "true").lower() == "true",
            issue_close_only_on_approved_promotion=os.environ.get("ACCRUVIA_ISSUE_CLOSE_ONLY_ON_APPROVED_PROMOTION", "false").lower() == "true",
            issue_reopen_on_pending=os.environ.get("ACCRUVIA_ISSUE_REOPEN_ON_PENDING", "true").lower() == "true",
            issue_reopen_on_active=os.environ.get("ACCRUVIA_ISSUE_REOPEN_ON_ACTIVE", "true").lower() == "true",
            issue_reopen_on_failed=os.environ.get("ACCRUVIA_ISSUE_REOPEN_ON_FAILED", "true").lower() == "true",
            timeout_ema_alpha=_env_float("ACCRUVIA_TIMEOUT_EMA_ALPHA", 0.5),
            timeout_min_seconds=_env_int("ACCRUVIA_TIMEOUT_MIN_SECONDS", 30),
            timeout_max_seconds=_env_int("ACCRUVIA_TIMEOUT_MAX_SECONDS", 1800),
            timeout_multiplier=_env_float("ACCRUVIA_TIMEOUT_MULTIPLIER", 2.5),
            memory_limit_mb=_env_int("ACCRUVIA_MEMORY_LIMIT_MB", 1024),
            cpu_time_limit_seconds=_env_int("ACCRUVIA_CPU_TIME_LIMIT_SECONDS", 300),
            observer_webhook_url=os.environ.get("ACCRUVIA_OBSERVER_WEBHOOK_URL"),
        )
