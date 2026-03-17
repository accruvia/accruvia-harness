from __future__ import annotations

import logging
import os
from dataclasses import asdict
from dataclasses import dataclass, field
import json
from pathlib import Path
from .domain import PromotionMode, RepoProvider, WorkspacePolicy

logger = logging.getLogger(__name__)

PERSISTED_CONFIG_FILENAME = "config.json"
PERSISTED_CONFIG_KEYS = frozenset(
    {
        "default_project_name",
        "default_repo",
        "runtime_backend",
        "temporal_target",
        "temporal_namespace",
        "temporal_task_queue",
        "worker_backend",
        "worker_command",
        "llm_backend",
        "llm_model",
        "llm_command",
        "llm_codex_command",
        "llm_claude_command",
        "llm_accruvia_client_command",
        "env_passthrough",
        "adapter_modules",
        "project_adapter_modules",
        "validator_modules",
        "cognition_modules",
        "telemetry_fsync_writes",
        "otel_service_name",
        "otel_exporter_otlp_endpoint",
        "issue_close_on_completed",
        "issue_close_only_on_approved_promotion",
        "issue_reopen_on_pending",
        "issue_reopen_on_active",
        "issue_reopen_on_failed",
        "timeout_ema_alpha",
        "timeout_min_seconds",
        "timeout_max_seconds",
        "timeout_multiplier",
        "heartbeat_timeout_seconds",
        "heartbeat_failure_escalation_threshold",
        "task_run_timeout_seconds",
        "task_llm_timeout_seconds",
        "task_validation_timeout_seconds",
        "task_validation_startup_timeout_seconds",
        "task_compile_timeout_seconds",
        "task_git_timeout_seconds",
        "task_stale_timeout_seconds",
        "memory_limit_mb",
        "cpu_time_limit_seconds",
        "observer_webhook_url",
        "default_workspace_policy",
        "default_promotion_mode",
        "default_repo_provider",
        "default_base_branch",
        "pr_check_enabled",
        "pr_check_interval_seconds",
    }
)


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


def harness_home() -> Path:
    return Path(os.environ.get("ACCRUVIA_HARNESS_HOME", ".accruvia-harness"))


def default_config_path(base: str | Path | None = None) -> Path:
    home = Path(base) if base is not None else harness_home()
    return home / PERSISTED_CONFIG_FILENAME


def load_persisted_config(path: str | Path) -> dict[str, object]:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unable to read persisted config from %s: %s", config_path, exc)
        return {}
    if not isinstance(payload, dict):
        logger.warning("Persisted config at %s is not a JSON object; ignoring it", config_path)
        return {}
    return {key: value for key, value in payload.items() if key in PERSISTED_CONFIG_KEYS}


def write_persisted_config(path: str | Path, payload: dict[str, object]) -> Path:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    filtered = {key: payload[key] for key in sorted(payload) if key in PERSISTED_CONFIG_KEYS}
    config_path.write_text(json.dumps(filtered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path


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
    cognition_modules: tuple[str, ...] = field(default_factory=tuple)
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
    heartbeat_timeout_seconds: int = 1800
    heartbeat_failure_escalation_threshold: int = 3
    task_run_timeout_seconds: int = 1800
    task_llm_timeout_seconds: int = 1800
    task_validation_timeout_seconds: int = 300
    task_validation_startup_timeout_seconds: int = 30
    task_compile_timeout_seconds: int = 120
    task_git_timeout_seconds: int = 30
    task_stale_timeout_seconds: int = 300
    memory_limit_mb: int = 1024
    cpu_time_limit_seconds: int = 300
    observer_webhook_url: str | None = None
    default_workspace_policy: str = WorkspacePolicy.ISOLATED_REQUIRED.value
    default_promotion_mode: str = PromotionMode.BRANCH_AND_PR.value
    default_repo_provider: str | None = RepoProvider.GITHUB.value
    default_base_branch: str = "main"
    pr_check_enabled: bool = True
    pr_check_interval_seconds: int = 28800

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        for path_key in ("db_path", "workspace_root", "log_path", "telemetry_dir"):
            payload[path_key] = str(payload[path_key])
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), sort_keys=True)

    def persisted_payload(self) -> dict[str, object]:
        payload = self.to_payload()
        return {key: payload[key] for key in PERSISTED_CONFIG_KEYS if key in payload}

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
            cognition_modules=tuple(str(item) for item in payload.get("cognition_modules", ())),
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
            heartbeat_timeout_seconds=int(payload.get("heartbeat_timeout_seconds", 1800)),
            heartbeat_failure_escalation_threshold=int(
                payload.get("heartbeat_failure_escalation_threshold", 3)
            ),
            task_run_timeout_seconds=int(payload.get("task_run_timeout_seconds", 1800)),
            task_llm_timeout_seconds=int(payload.get("task_llm_timeout_seconds", 1800)),
            task_validation_timeout_seconds=int(payload.get("task_validation_timeout_seconds", 300)),
            task_validation_startup_timeout_seconds=int(payload.get("task_validation_startup_timeout_seconds", 30)),
            task_compile_timeout_seconds=int(payload.get("task_compile_timeout_seconds", 120)),
            task_git_timeout_seconds=int(payload.get("task_git_timeout_seconds", 30)),
            task_stale_timeout_seconds=int(payload.get("task_stale_timeout_seconds", 300)),
            memory_limit_mb=int(payload.get("memory_limit_mb", 1024)),
            cpu_time_limit_seconds=int(payload.get("cpu_time_limit_seconds", 300)),
            observer_webhook_url=(
                str(payload["observer_webhook_url"])
                if payload.get("observer_webhook_url") is not None
                else None
            ),
            default_workspace_policy=str(payload.get("default_workspace_policy", WorkspacePolicy.ISOLATED_REQUIRED.value)),
            default_promotion_mode=str(payload.get("default_promotion_mode", PromotionMode.BRANCH_AND_PR.value)),
            default_repo_provider=(
                str(payload["default_repo_provider"]) if payload.get("default_repo_provider") is not None else None
            ),
            default_base_branch=str(payload.get("default_base_branch", "main")),
            pr_check_enabled=bool(payload.get("pr_check_enabled", True)),
            pr_check_interval_seconds=int(payload.get("pr_check_interval_seconds", 28800)),
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
        config_file: str | Path | None = None,
    ) -> "HarnessConfig":
        base = harness_home()
        resolved_db = Path(db_path) if db_path is not None else base / "harness.db"
        resolved_workspace = (
            Path(workspace_root) if workspace_root is not None else base / "workspace"
        )
        resolved_log = Path(log_path) if log_path is not None else base / "harness.log"
        resolved_telemetry = base / "telemetry"
        resolved_config = Path(config_file) if config_file is not None else Path(
            os.environ.get("ACCRUVIA_HARNESS_CONFIG", default_config_path(base))
        )
        payload = cls(
            db_path=resolved_db,
            workspace_root=resolved_workspace,
            log_path=resolved_log,
            telemetry_dir=resolved_telemetry,
            telemetry_fsync_writes=False,
            otel_service_name="accruvia-harness",
            otel_exporter_otlp_endpoint=None,
            default_project_name="accruvia",
            default_repo="soverton/accruvia",
            runtime_backend="local",
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="accruvia-harness",
            worker_backend="local",
            worker_command=None,
            llm_backend="auto",
            llm_model=None,
            llm_command=None,
            llm_codex_command=None,
            llm_claude_command=None,
            llm_accruvia_client_command=None,
        ).to_payload()
        payload.update(load_persisted_config(resolved_config))
        payload.update(
            {
                "db_path": str(resolved_db),
                "workspace_root": str(resolved_workspace),
                "log_path": str(resolved_log),
                "telemetry_dir": str(resolved_telemetry),
                "telemetry_fsync_writes": os.environ.get("ACCRUVIA_TELEMETRY_FSYNC_WRITES", str(payload.get("telemetry_fsync_writes", False))).lower() == "true",
                "otel_service_name": os.environ.get("ACCRUVIA_OTEL_SERVICE_NAME", str(payload.get("otel_service_name", "accruvia-harness"))),
                "otel_exporter_otlp_endpoint": os.environ.get("ACCRUVIA_OTEL_EXPORTER_OTLP_ENDPOINT") or payload.get("otel_exporter_otlp_endpoint"),
                "default_project_name": os.environ.get("ACCRUVIA_HARNESS_PROJECT", str(payload.get("default_project_name", "accruvia"))),
                "default_repo": os.environ.get("ACCRUVIA_HARNESS_REPO", str(payload.get("default_repo", "soverton/accruvia"))),
                "runtime_backend": os.environ.get("ACCRUVIA_HARNESS_RUNTIME", str(payload.get("runtime_backend", "local"))),
                "temporal_target": os.environ.get("ACCRUVIA_TEMPORAL_TARGET", str(payload.get("temporal_target", "localhost:7233"))),
                "temporal_namespace": os.environ.get("ACCRUVIA_TEMPORAL_NAMESPACE", str(payload.get("temporal_namespace", "default"))),
                "temporal_task_queue": os.environ.get("ACCRUVIA_TEMPORAL_TASK_QUEUE", str(payload.get("temporal_task_queue", "accruvia-harness"))),
                "worker_backend": os.environ.get("ACCRUVIA_WORKER_BACKEND", str(payload.get("worker_backend", "local"))),
                "worker_command": os.environ.get("ACCRUVIA_WORKER_COMMAND", payload.get("worker_command")),
                "llm_backend": os.environ.get("ACCRUVIA_LLM_BACKEND", str(payload.get("llm_backend", "auto"))),
                "llm_model": os.environ.get("ACCRUVIA_LLM_MODEL", payload.get("llm_model")),
                "llm_command": os.environ.get("ACCRUVIA_LLM_COMMAND", payload.get("llm_command")),
                "llm_codex_command": os.environ.get("ACCRUVIA_LLM_CODEX_COMMAND", payload.get("llm_codex_command")),
                "llm_claude_command": os.environ.get("ACCRUVIA_LLM_CLAUDE_COMMAND", payload.get("llm_claude_command")),
                "llm_accruvia_client_command": os.environ.get(
                    "ACCRUVIA_LLM_ACCRUVIA_CLIENT_COMMAND",
                    payload.get("llm_accruvia_client_command"),
                ),
                "env_passthrough": tuple(
                    item.strip()
                    for item in os.environ.get(
                        "ACCRUVIA_ENV_PASSTHROUGH",
                        ",".join(str(item) for item in payload.get("env_passthrough", ())),
                    ).split(",")
                    if item.strip()
                ),
                "adapter_modules": tuple(
                    item.strip()
                    for item in os.environ.get(
                        "ACCRUVIA_ADAPTER_MODULES",
                        ",".join(str(item) for item in payload.get("adapter_modules", ())),
                    ).split(",")
                    if item.strip()
                ),
                "project_adapter_modules": tuple(
                    item.strip()
                    for item in os.environ.get(
                        "ACCRUVIA_PROJECT_ADAPTER_MODULES",
                        ",".join(str(item) for item in payload.get("project_adapter_modules", ())),
                    ).split(",")
                    if item.strip()
                ),
                "validator_modules": tuple(
                    item.strip()
                    for item in os.environ.get(
                        "ACCRUVIA_VALIDATOR_MODULES",
                        ",".join(str(item) for item in payload.get("validator_modules", ())),
                    ).split(",")
                    if item.strip()
                ),
                "cognition_modules": tuple(
                    item.strip()
                    for item in os.environ.get(
                        "ACCRUVIA_COGNITION_MODULES",
                        ",".join(str(item) for item in payload.get("cognition_modules", ())),
                    ).split(",")
                    if item.strip()
                ),
                "issue_close_on_completed": os.environ.get(
                    "ACCRUVIA_ISSUE_CLOSE_ON_COMPLETED",
                    str(payload.get("issue_close_on_completed", True)),
                ).lower() == "true",
                "issue_close_only_on_approved_promotion": os.environ.get(
                    "ACCRUVIA_ISSUE_CLOSE_ONLY_ON_APPROVED_PROMOTION",
                    str(payload.get("issue_close_only_on_approved_promotion", False)),
                ).lower() == "true",
                "issue_reopen_on_pending": os.environ.get(
                    "ACCRUVIA_ISSUE_REOPEN_ON_PENDING",
                    str(payload.get("issue_reopen_on_pending", True)),
                ).lower() == "true",
                "issue_reopen_on_active": os.environ.get(
                    "ACCRUVIA_ISSUE_REOPEN_ON_ACTIVE",
                    str(payload.get("issue_reopen_on_active", True)),
                ).lower() == "true",
                "issue_reopen_on_failed": os.environ.get(
                    "ACCRUVIA_ISSUE_REOPEN_ON_FAILED",
                    str(payload.get("issue_reopen_on_failed", True)),
                ).lower() == "true",
                "timeout_ema_alpha": _env_float(
                    "ACCRUVIA_TIMEOUT_EMA_ALPHA",
                    float(payload.get("timeout_ema_alpha", 0.5)),
                ),
                "timeout_min_seconds": _env_int(
                    "ACCRUVIA_TIMEOUT_MIN_SECONDS",
                    int(payload.get("timeout_min_seconds", 30)),
                ),
                "timeout_max_seconds": _env_int(
                    "ACCRUVIA_TIMEOUT_MAX_SECONDS",
                    int(payload.get("timeout_max_seconds", 1800)),
                ),
                "timeout_multiplier": _env_float(
                    "ACCRUVIA_TIMEOUT_MULTIPLIER",
                    float(payload.get("timeout_multiplier", 2.5)),
                ),
                "heartbeat_timeout_seconds": _env_int(
                    "ACCRUVIA_HEARTBEAT_TIMEOUT_SECONDS",
                    int(payload.get("heartbeat_timeout_seconds", 1800)),
                ),
                "heartbeat_failure_escalation_threshold": _env_int(
                    "ACCRUVIA_HEARTBEAT_FAILURE_ESCALATION_THRESHOLD",
                    int(payload.get("heartbeat_failure_escalation_threshold", 3)),
                ),
                "task_run_timeout_seconds": _env_int(
                    "ACCRUVIA_TASK_RUN_TIMEOUT_SECONDS",
                    int(payload.get("task_run_timeout_seconds", 1800)),
                ),
                "task_llm_timeout_seconds": _env_int(
                    "ACCRUVIA_TASK_LLM_TIMEOUT_SECONDS",
                    int(payload.get("task_llm_timeout_seconds", 1800)),
                ),
                "task_validation_timeout_seconds": _env_int(
                    "ACCRUVIA_TASK_VALIDATION_TIMEOUT_SECONDS",
                    int(payload.get("task_validation_timeout_seconds", 300)),
                ),
                "task_validation_startup_timeout_seconds": _env_int(
                    "ACCRUVIA_TASK_VALIDATION_STARTUP_TIMEOUT_SECONDS",
                    int(payload.get("task_validation_startup_timeout_seconds", 30)),
                ),
                "task_compile_timeout_seconds": _env_int(
                    "ACCRUVIA_TASK_COMPILE_TIMEOUT_SECONDS",
                    int(payload.get("task_compile_timeout_seconds", 120)),
                ),
                "task_git_timeout_seconds": _env_int(
                    "ACCRUVIA_TASK_GIT_TIMEOUT_SECONDS",
                    int(payload.get("task_git_timeout_seconds", 30)),
                ),
                "task_stale_timeout_seconds": _env_int(
                    "ACCRUVIA_TASK_STALE_TIMEOUT_SECONDS",
                    int(payload.get("task_stale_timeout_seconds", 300)),
                ),
                "memory_limit_mb": _env_int(
                    "ACCRUVIA_MEMORY_LIMIT_MB",
                    int(payload.get("memory_limit_mb", 1024)),
                ),
                "cpu_time_limit_seconds": _env_int(
                    "ACCRUVIA_CPU_TIME_LIMIT_SECONDS",
                    int(payload.get("cpu_time_limit_seconds", 300)),
                ),
                "observer_webhook_url": os.environ.get(
                    "ACCRUVIA_OBSERVER_WEBHOOK_URL",
                    payload.get("observer_webhook_url"),
                ),
                "default_workspace_policy": os.environ.get(
                    "ACCRUVIA_DEFAULT_WORKSPACE_POLICY",
                    str(payload.get("default_workspace_policy", WorkspacePolicy.ISOLATED_REQUIRED.value)),
                ),
                "default_promotion_mode": os.environ.get(
                    "ACCRUVIA_DEFAULT_PROMOTION_MODE",
                    str(payload.get("default_promotion_mode", PromotionMode.BRANCH_AND_PR.value)),
                ),
                "default_repo_provider": os.environ.get(
                    "ACCRUVIA_DEFAULT_REPO_PROVIDER",
                    str(payload.get("default_repo_provider", RepoProvider.GITHUB.value)),
                ),
                "default_base_branch": os.environ.get(
                    "ACCRUVIA_DEFAULT_BASE_BRANCH",
                    str(payload.get("default_base_branch", "main")),
                ),
                "pr_check_enabled": os.environ.get(
                    "ACCRUVIA_PR_CHECK_ENABLED",
                    str(payload.get("pr_check_enabled", True)),
                ).lower() == "true",
                "pr_check_interval_seconds": _env_int(
                    "ACCRUVIA_PR_CHECK_INTERVAL_SECONDS",
                    int(payload.get("pr_check_interval_seconds", 28800)),
                ),
            }
        )
        return cls.from_payload(payload)
