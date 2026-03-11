from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..config import HarnessConfig
from ..domain import serialize_dataclass
from ..engine import HarnessEngine
from ..github import GitHubCLI
from ..gitlab import GitLabCLI
from ..interrogation import HarnessQueryService
from ..project_adapters import build_project_adapter_registry
from ..runtime import WorkflowRuntime, build_runtime
from ..store import SQLiteHarnessStore
from ..telemetry import TelemetrySink
from ..validation import build_validator_registry
from ..llm import build_llm_router
from ..services.issue_policy import IssueStatePolicy
from ..workers import build_worker_from_config


def emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


@dataclass(slots=True)
class CLIContext:
    config: HarnessConfig
    store: SQLiteHarnessStore
    engine: HarnessEngine
    github: GitHubCLI
    gitlab: GitLabCLI
    runtime: WorkflowRuntime
    query_service: HarnessQueryService
    telemetry: TelemetrySink


def build_context(config: HarnessConfig) -> CLIContext:
    store = SQLiteHarnessStore(config.db_path)
    store.initialize()
    telemetry = TelemetrySink(config.telemetry_dir)
    issue_policy = IssueStatePolicy(
        close_on_completed=config.issue_close_on_completed,
        close_only_on_approved_promotion=config.issue_close_only_on_approved_promotion,
        reopen_on_pending=config.issue_reopen_on_pending,
        reopen_on_active=config.issue_reopen_on_active,
        reopen_on_failed=config.issue_reopen_on_failed,
    )
    engine = HarnessEngine(
        store=store,
        workspace_root=config.workspace_root,
        project_adapter_registry=build_project_adapter_registry(config.project_adapter_modules),
        validator_registry=build_validator_registry(config.validator_modules),
        telemetry=telemetry,
        issue_state_policy=issue_policy,
    )
    engine.set_llm_router(build_llm_router(config))
    engine.set_worker(build_worker_from_config(config))
    return CLIContext(
        config=config,
        store=store,
        engine=engine,
        github=GitHubCLI(),
        gitlab=GitLabCLI(),
        runtime=build_runtime(
            backend=config.runtime_backend,
            engine=engine,
            temporal_target=config.temporal_target,
            temporal_namespace=config.temporal_namespace,
            temporal_task_queue=config.temporal_task_queue,
        ),
        query_service=HarnessQueryService(store),
        telemetry=telemetry,
    )
