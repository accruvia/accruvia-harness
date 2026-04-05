"""SkillsWorker — a WorkerBackend that drives the skills pipeline.

Drop-in replacement for LocalArtifactWorker and CommandWorker. Implements
the WorkerBackend protocol so it plugs into RunService without signature
changes. Internally delegates to SkillsWorkOrchestrator which runs the
scope/implement/self-review/validate/diagnose pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .domain import Run, Task
from .llm import LLMRouter
from .policy import WorkResult
from .services.work_orchestrator import SkillsWorkOrchestrator
from .skills import SkillRegistry, build_default_registry


def _prepared_project_workspace(run_dir: Path) -> Path:
    workspace = run_dir / "workspace"
    return workspace if workspace.exists() else run_dir


class SkillsWorker:
    """WorkerBackend backed by the skills pipeline."""

    def __init__(
        self,
        llm_router: LLMRouter,
        skill_registry: SkillRegistry | None = None,
        workspace_root: Path | None = None,
        telemetry: Any = None,
    ) -> None:
        self.llm_router = llm_router
        self.skill_registry = skill_registry or build_default_registry()
        self.workspace_root = Path(workspace_root) if workspace_root is not None else Path(".accruvia-harness") / "workspace"
        self.telemetry = telemetry
        self._orchestrator = SkillsWorkOrchestrator(
            skill_registry=self.skill_registry,
            llm_router=self.llm_router,
            workspace_root=self.workspace_root,
            telemetry=self.telemetry,
        )
        self._progress_callback = None

    def set_progress_callback(self, callback) -> None:
        self._progress_callback = callback

    def work(
        self,
        task: Task,
        run: Run,
        workspace_root: Path,
        retry_hints: dict | None = None,
    ) -> WorkResult:
        workspace_root = Path(workspace_root)
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        project_workspace = _prepared_project_workspace(run_dir)

        # retry_hints carries prior-run context from run_service. Fall back to
        # legacy dynamic attribute for any caller that hasn't migrated.
        hints = retry_hints if isinstance(retry_hints, dict) else {}
        if not hints:
            legacy = getattr(run, "retry_hints", None)
            if isinstance(legacy, dict):
                hints = legacy
        retry_feedback = str(
            hints.get("review_feedback") or hints.get("retry_feedback")
            or hints.get("scope_adjustment") or ""
        )
        prior_scope = hints.get("prior_scope")

        if self._progress_callback is not None:
            self._progress_callback(
                {
                    "type": "worker_phase",
                    "worker_phase": "scoping",
                    "task_id": task.id,
                    "run_id": run.id,
                }
            )

        result = self._orchestrator.execute(
            task=task,
            run=run,
            workspace=project_workspace,
            run_dir=run_dir,
            retry_feedback=retry_feedback,
            prior_scope=prior_scope,
        )

        if self._progress_callback is not None:
            stage = "complete"
            if result.diagnostics:
                stage = str(result.diagnostics.get("stage") or "complete")
            self._progress_callback(
                {
                    "type": "worker_phase",
                    "worker_phase": stage,
                    "task_id": task.id,
                    "run_id": run.id,
                    "outcome": result.outcome,
                }
            )
        return result
