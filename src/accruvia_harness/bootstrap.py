from __future__ import annotations

from .cognition import build_cognition_registry
from .config import HarnessConfig
from .engine import HarnessEngine
from .llm import build_llm_router
from .project_adapters import build_project_adapter_registry
from .services.issue_policy import IssueStatePolicy
from .store import SQLiteHarnessStore
from .telemetry import TelemetrySink
from .validation import build_validator_registry
from .routing_hook import RoutingHook
from .workers import build_worker_from_config


def build_store(config: HarnessConfig) -> SQLiteHarnessStore:
    store = SQLiteHarnessStore(config.db_path)
    store.initialize()
    store.observer_webhook_url = config.observer_webhook_url
    return store


def build_telemetry(config: HarnessConfig) -> TelemetrySink:
    return TelemetrySink(
        config.telemetry_dir,
        service_name=config.otel_service_name,
        otlp_endpoint=config.otel_exporter_otlp_endpoint,
        fsync_writes=config.telemetry_fsync_writes,
    )


def build_issue_state_policy(config: HarnessConfig) -> IssueStatePolicy:
    return IssueStatePolicy(
        close_on_completed=config.issue_close_on_completed,
        close_only_on_approved_promotion=config.issue_close_only_on_approved_promotion,
        reopen_on_pending=config.issue_reopen_on_pending,
        reopen_on_active=config.issue_reopen_on_active,
        reopen_on_failed=config.issue_reopen_on_failed,
    )


def build_engine_from_config(
    config: HarnessConfig,
    *,
    store: SQLiteHarnessStore | None = None,
    telemetry: TelemetrySink | None = None,
) -> HarnessEngine:
    resolved_store = store or build_store(config)
    resolved_telemetry = telemetry or build_telemetry(config)
    engine = HarnessEngine(
        store=resolved_store,
        workspace_root=config.workspace_root,
        project_adapter_registry=build_project_adapter_registry(config.project_adapter_modules),
        validator_registry=build_validator_registry(config.validator_modules),
        cognition_registry=build_cognition_registry(config.cognition_modules),
        heartbeat_timeout_seconds=config.heartbeat_timeout_seconds,
        heartbeat_failure_escalation_threshold=config.heartbeat_failure_escalation_threshold,
        telemetry=resolved_telemetry,
        issue_state_policy=build_issue_state_policy(config),
    )
    engine.set_llm_router(build_llm_router(config, telemetry=resolved_telemetry))
    routing_hook = RoutingHook.from_config(config)
    engine.set_worker(build_worker_from_config(config, telemetry=resolved_telemetry, routing_hook=routing_hook))
    return engine
