from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..bootstrap import build_engine_from_config, build_store, build_telemetry
from ..config import HarnessConfig
from ..domain import serialize_dataclass
from ..engine import HarnessEngine
from ..github import GitHubCLI
from ..gitlab import GitLabCLI
from ..interrogation import HarnessQueryService, InterrogationService
from ..runtime import WorkflowRuntime, build_runtime
from ..store import SQLiteHarnessStore
from ..telemetry import TelemetrySink


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
    interrogation_service: InterrogationService
    telemetry: TelemetrySink


def build_context(config: HarnessConfig) -> CLIContext:
    store = build_store(config)
    telemetry = build_telemetry(config)
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
    )
