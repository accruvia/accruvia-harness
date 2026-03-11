from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Protocol

from .adapters import AdapterRegistry, build_adapter_registry
from .config import HarnessConfig
from .domain import Run, Task
from .llm import LLMExecutionError, LLMInvocation, LLMRouter, build_llm_router
from .policy import WorkResult


class WorkerBackend(Protocol):
    def work(self, task: Task, run: Run, workspace_root: Path) -> WorkResult: ...


class WorkerExecutionError(RuntimeError):
    """Raised when a worker backend fails to produce a usable result."""


def _prepared_project_workspace(run_dir: Path) -> Path:
    workspace = run_dir / "workspace"
    return workspace if workspace.exists() else run_dir


class LocalArtifactWorker:
    def __init__(self, adapter_registry: AdapterRegistry | None = None) -> None:
        self.adapter_registry = adapter_registry or build_adapter_registry()

    def work(self, task: Task, run: Run, workspace_root: Path) -> WorkResult:
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


class CommandWorker:
    def __init__(self, command: str, backend_name: str) -> None:
        self.command = command
        self.backend_name = backend_name

    def work(self, task: Task, run: Run, workspace_root: Path) -> WorkResult:
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        project_workspace = _prepared_project_workspace(run_dir)
        env = {
            "ACCRUVIA_TASK_ID": task.id,
            "ACCRUVIA_RUN_ID": run.id,
            "ACCRUVIA_TASK_OBJECTIVE": task.objective,
            "ACCRUVIA_RUN_SUMMARY": run.summary,
            "ACCRUVIA_RUN_DIR": str(run_dir),
            "ACCRUVIA_PROJECT_WORKSPACE": str(project_workspace),
        }
        completed = subprocess.run(
            self.command,
            shell=True,
            check=False,
            cwd=project_workspace,
            capture_output=True,
            text=True,
            env={**os.environ, **env},
        )
        stdout_path = run_dir / "worker.stdout.txt"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path = run_dir / "worker.stderr.txt"
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        report_path = run_dir / "report.json"
        payload: dict[str, object] = {}
        if report_path.exists():
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
        report_path.write_text(
            json.dumps(
                {
                    **payload,
                    "task_id": task.id,
                    "run_id": run.id,
                    "attempt": run.attempt,
                    "strategy": task.strategy,
                    "objective": task.objective,
                    "worker_backend": self.backend_name,
                    "validation_profile": task.validation_profile,
                    "command": self.command,
                    "returncode": completed.returncode,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        outcome = "success" if completed.returncode == 0 else "failed"
        return WorkResult(
            summary=f"Executed {self.backend_name} worker command and captured output.",
            artifacts=[
                ("worker_stdout", str(stdout_path), "Captured shell worker stdout"),
                ("worker_stderr", str(stderr_path), "Captured shell worker stderr"),
                ("report", str(report_path), "Structured run report"),
            ],
            outcome=outcome,
            diagnostics={
                "worker_backend": self.backend_name,
                "command": self.command,
                "returncode": completed.returncode,
                "project_workspace": str(project_workspace),
            },
        )


class ShellCommandWorker(CommandWorker):
    def __init__(self, command: str) -> None:
        super().__init__(command=command, backend_name="shell")


class AgentCommandWorker(CommandWorker):
    def __init__(self, command: str) -> None:
        super().__init__(command=command, backend_name="agent")


class LLMTaskWorker:
    def __init__(self, router: LLMRouter, model: str | None = None) -> None:
        self.router = router
        self.model = model

    def work(self, task: Task, run: Run, workspace_root: Path) -> WorkResult:
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        project_workspace = _prepared_project_workspace(run_dir)
        prompt = self._build_prompt(task, run)
        executor, routed_backend = self.router.resolve()
        try:
            result = executor.execute(
                invocation=LLMInvocation(
                    task=task, run=run, prompt=prompt, run_dir=project_workspace, model=self.model
                )
            )
            outcome = "success"
            summary = f"Executed routed LLM worker via {routed_backend}."
            diagnostics = {
                **result.diagnostics,
                "worker_backend": "llm",
                "llm_backend": result.backend,
                "llm_model": self.model,
            }
        except LLMExecutionError as exc:
            error_path = run_dir / "llm_error.txt"
            error_path.write_text(str(exc), encoding="utf-8")
            report_path = run_dir / "report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "task_id": task.id,
                        "run_id": run.id,
                        "attempt": run.attempt,
                        "strategy": task.strategy,
                        "objective": task.objective,
                    "worker_backend": "llm",
                    "llm_backend": routed_backend,
                    "error": str(exc),
                    "validation_profile": task.validation_profile,
                    "project_workspace": str(project_workspace),
                },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            return WorkResult(
                summary=f"LLM worker failed via {routed_backend}.",
                artifacts=[
                    ("report", str(report_path), "Structured run report"),
                    ("llm_error", str(error_path), "LLM execution failure"),
                ],
                outcome="failed",
                diagnostics={
                    "worker_backend": "llm",
                    "llm_backend": routed_backend,
                    "llm_model": self.model,
                    "error": str(exc),
                    "project_workspace": str(project_workspace),
                },
            )

        report_path = run_dir / "report.json"
        payload: dict[str, object] = {}
        if report_path.exists():
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
        report_path.write_text(
            json.dumps(
                {
                    **payload,
                    "task_id": task.id,
                    "run_id": run.id,
                    "attempt": run.attempt,
                    "strategy": task.strategy,
                    "objective": task.objective,
                    "worker_backend": "llm",
                    "llm_backend": result.backend,
                    "llm_model": self.model,
                    "validation_profile": task.validation_profile,
                    "project_workspace": str(project_workspace),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return WorkResult(
            summary=summary,
            artifacts=[
                ("plan", str(result.prompt_path), "Prompt sent to the routed LLM executor"),
                ("llm_response", str(result.response_path), "LLM response artifact"),
                ("report", str(report_path), "Structured run report"),
            ],
            outcome=outcome,
            diagnostics=diagnostics,
        )

    def _build_prompt(self, task: Task, run: Run) -> str:
        return (
            f"Task: {task.title}\n"
            f"Objective: {task.objective}\n"
            f"Strategy: {task.strategy}\n"
            f"Task ID: {task.id}\n"
            f"Run ID: {run.id}\n"
            f"Attempt: {run.attempt}\n"
            f"Plan Summary: {run.summary}\n"
            "Instructions:\n"
            "- Produce the work needed for the objective.\n"
            "- Preserve durable artifacts in the run directory when appropriate.\n"
            "- Favor test-driven implementation when changing software behavior.\n"
        )


def build_worker(backend: str, shell_command: str | None = None) -> WorkerBackend:
    if backend == "local":
        return LocalArtifactWorker()
    if backend == "shell":
        if not shell_command:
            raise ValueError("Shell worker backend requires ACCRUVIA_WORKER_COMMAND")
        return ShellCommandWorker(shell_command)
    if backend == "agent":
        if not shell_command:
            raise ValueError("Agent worker backend requires ACCRUVIA_WORKER_COMMAND")
        return AgentCommandWorker(shell_command)
    raise ValueError(f"Unsupported worker backend: {backend}")


def build_worker_from_config(config: HarnessConfig) -> WorkerBackend:
    adapter_registry = build_adapter_registry(config.adapter_modules)
    if config.worker_backend == "llm":
        return LLMTaskWorker(build_llm_router(config), model=config.llm_model)
    if config.worker_backend == "local":
        return LocalArtifactWorker(adapter_registry=adapter_registry)
    return build_worker(config.worker_backend, config.worker_command)
