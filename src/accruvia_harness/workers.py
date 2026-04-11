from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .adapters import AdapterRegistry, build_adapter_registry
from .config import HarnessConfig
from .domain import Run, Task
from .llm import build_llm_router
from .policy import WorkResult


class WorkerBackend(Protocol):
    def work(
        self,
        task: Task,
        run: Run,
        workspace_root: Path,
        retry_hints: dict[str, object] | None = None,
    ) -> WorkResult: ...


class WorkerExecutionError(RuntimeError):
    """Raised when a worker backend fails to produce a usable result."""


def _prepared_project_workspace(run_dir: Path) -> Path:
    workspace = run_dir / "workspace"
    return workspace if workspace.exists() else run_dir


class LocalArtifactWorker:
    def __init__(self, adapter_registry: AdapterRegistry | None = None) -> None:
        self.adapter_registry = adapter_registry or build_adapter_registry()

    def work(self, task: Task, run: Run, workspace_root: Path, retry_hints: dict | None = None) -> WorkResult:
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        project_workspace = _prepared_project_workspace(run_dir)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text(
            f"task={task.id}\nrun={run.id}\nattempt={run.attempt}\nobjective={task.objective}\nproject_workspace={project_workspace}\n",
            encoding="utf-8",
        )
        adapter = self.adapter_registry.get(task.validation_profile)
        evidence = adapter.build_evidence(task, project_workspace)
        report_path = run_dir / "report.json"
        report_path.write_text(
            json.dumps(
                {
                    "task_id": task.id,
                    "run_id": run.id,
                    "attempt": run.attempt,
                    "strategy": task.strategy,
                    "objective": task.objective,
                    "worker_backend": "local",
                    "validation_profile": task.validation_profile,
                    "worker_outcome": "success" if evidence.passed else "failed",
                    **evidence.report,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return WorkResult(
            summary="Recorded durable plan and report artifacts for the run.",
            artifacts=[
                ("plan", str(plan_path), "Run planning artifact"),
                ("report", str(report_path), "Structured run report"),
            ],
            outcome="success" if evidence.passed else "failed",
            diagnostics={
                "worker_backend": "local",
                "validation_profile": task.validation_profile,
                "project_workspace": str(project_workspace),
                **evidence.diagnostics,
            },
        )


def build_worker_from_config(config: HarnessConfig, telemetry=None) -> WorkerBackend:
    """Skills is the only worker backend. Agent backend was removed in pre-alpha."""
    from .skills_worker import SkillsWorker

    return SkillsWorker(
        llm_router=build_llm_router(config, telemetry=telemetry),
        workspace_root=config.workspace_root,
        telemetry=telemetry,
    )
