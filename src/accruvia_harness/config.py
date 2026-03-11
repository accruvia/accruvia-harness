from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


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
    adapter_modules: tuple[str, ...] = field(default_factory=tuple)

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
        return cls(
            db_path=resolved_db,
            workspace_root=resolved_workspace,
            log_path=resolved_log,
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
            adapter_modules=tuple(
                item.strip()
                for item in os.environ.get("ACCRUVIA_ADAPTER_MODULES", "").split(",")
                if item.strip()
            ),
        )
