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
from ..runtime import WorkflowRuntime, build_runtime
from ..store import SQLiteHarnessStore
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


def build_context(config: HarnessConfig) -> CLIContext:
    store = SQLiteHarnessStore(config.db_path)
    store.initialize()
    engine = HarnessEngine(store=store, workspace_root=config.workspace_root)
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
    )
