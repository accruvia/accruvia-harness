from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from .adapters import AdapterRegistry, build_adapter_registry
from .config import HarnessConfig
from .domain import Run, Task
from .llm import LLMExecutionError, LLMInvocation, LLMRouter, build_llm_router
from .policy import WorkResult
from .subprocess_env import build_subprocess_env


class WorkerBackend(Protocol):
    def work(self, task: Task, run: Run, workspace_root: Path) -> WorkResult: ...


class WorkerExecutionError(RuntimeError):
    """Raised when a worker backend fails to produce a usable result."""


def _coerce_subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _prepared_project_workspace(run_dir: Path) -> Path:
    workspace = run_dir / "workspace"
    return workspace if workspace.exists() else run_dir


def _default_agent_worker_command() -> str:
    script_path = Path(__file__).resolve().parents[2] / "bin" / "accruvia-codex-worker"
    if script_path.exists():
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(script_path))}"
    return f"{shlex.quote(sys.executable)} -m accruvia_harness.agent_worker"


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


class CommandWorker:
    def __init__(
        self,
        command: str,
        backend_name: str,
        timeout_policy=None,
        resource_policy=None,
        env_passthrough: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.backend_name = backend_name
        self.timeout_policy = timeout_policy
        self.resource_policy = resource_policy
        self.env_passthrough = env_passthrough
        self.extra_env = dict(extra_env or {})

    def work(self, task: Task, run: Run, workspace_root: Path) -> WorkResult:
        run_dir = (workspace_root / "runs" / run.id).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        project_workspace = _prepared_project_workspace(run_dir).resolve()
        env = {
            "ACCRUVIA_TASK_ID": task.id,
            "ACCRUVIA_RUN_ID": run.id,
            "ACCRUVIA_TASK_OBJECTIVE": task.objective,
            "ACCRUVIA_RUN_SUMMARY": run.summary,
            "ACCRUVIA_RUN_DIR": str(run_dir),
            "ACCRUVIA_PROJECT_WORKSPACE": str(project_workspace),
            "ACCRUVIA_TASK_SCOPE_JSON": json.dumps(task.scope, sort_keys=True),
            **self.extra_env,
        }
        timeout_seconds = None
        if self.timeout_policy is not None:
            timeout_seconds = self.timeout_policy.timeout_seconds(
                task.validation_profile, self.backend_name
            )
        try:
            completed = subprocess.run(
                self.command,
                shell=True,
                check=False,
                cwd=project_workspace,
                capture_output=True,
                text=True,
                env=build_subprocess_env(env, passthrough=self.env_passthrough),
                timeout=timeout_seconds,
                preexec_fn=self.resource_policy.preexec_fn() if self.resource_policy is not None else None,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path = run_dir / "worker.stdout.txt"
            stdout_path.write_text(_coerce_subprocess_output(exc.stdout), encoding="utf-8")
            stderr_path = run_dir / "worker.stderr.txt"
            stderr_path.write_text(_coerce_subprocess_output(exc.stderr), encoding="utf-8")
            report_path = run_dir / "report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "task_id": task.id,
                        "run_id": run.id,
                        "attempt": run.attempt,
                        "strategy": task.strategy,
                        "objective": task.objective,
                        "worker_backend": self.backend_name,
                        "validation_profile": task.validation_profile,
                        "command": self.command,
                        "timeout_seconds": timeout_seconds,
                        "timed_out": True,
                        "worker_outcome": "blocked",
                        "blocked": True,
                        "infrastructure_failure": True,
                        "failure_category": "executor_timeout",
                        "memory_limit_mb": getattr(self.resource_policy, "memory_limit_mb", None),
                        "cpu_time_limit_seconds": getattr(self.resource_policy, "cpu_time_limit_seconds", None),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            return WorkResult(
                summary=f"Executed {self.backend_name} worker command and timed out before task work completed.",
                artifacts=[
                    ("worker_stdout", str(stdout_path), "Captured shell worker stdout"),
                    ("worker_stderr", str(stderr_path), "Captured shell worker stderr"),
                    ("report", str(report_path), "Structured run report"),
                ],
                outcome="blocked",
                diagnostics={
                    "worker_backend": self.backend_name,
                    "command": self.command,
                    "timed_out": True,
                    "blocked": True,
                    "infrastructure_failure": True,
                    "failure_category": "executor_timeout",
                    "timeout_seconds": timeout_seconds,
                    "memory_limit_mb": getattr(self.resource_policy, "memory_limit_mb", None),
                    "cpu_time_limit_seconds": getattr(self.resource_policy, "cpu_time_limit_seconds", None),
                    "project_workspace": str(project_workspace),
                },
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
        reported_outcome = payload.get("worker_outcome")
        blocked = payload.get("blocked") is True or payload.get("promotion_blocked") is True
        infrastructure_failure = bool(payload.get("infrastructure_failure"))
        if isinstance(reported_outcome, str) and reported_outcome in {"success", "failed", "blocked"}:
            outcome = reported_outcome
        elif blocked:
            outcome = "blocked"
        elif completed.returncode != 0 and not payload:
            outcome = "blocked"
            infrastructure_failure = True
            payload = {
                "worker_outcome": "blocked",
                "blocked": True,
                "infrastructure_failure": True,
                "failure_category": "executor_process_failure",
                "failure_message": (
                    completed.stderr.strip() or completed.stdout.strip() or f"{self.backend_name} worker exited non-zero"
                ),
            }
        else:
            outcome = "success" if completed.returncode == 0 else "failed"
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
                    "memory_limit_mb": getattr(self.resource_policy, "memory_limit_mb", None),
                    "cpu_time_limit_seconds": getattr(self.resource_policy, "cpu_time_limit_seconds", None),
                    "worker_outcome": outcome,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        plan_artifact = []
        plan_path = run_dir / "plan.txt"
        if plan_path.exists():
            plan_artifact.append(("plan", str(plan_path), "Structured plan artifact"))
        return WorkResult(
            summary=f"Executed {self.backend_name} worker command and captured output.",
            artifacts=[
                *plan_artifact,
                ("worker_stdout", str(stdout_path), "Captured shell worker stdout"),
                ("worker_stderr", str(stderr_path), "Captured shell worker stderr"),
                ("report", str(report_path), "Structured run report"),
            ],
            outcome=outcome,
            diagnostics={
                "worker_backend": self.backend_name,
                "command": self.command,
                "returncode": completed.returncode,
                "blocked": outcome == "blocked",
                "infrastructure_failure": infrastructure_failure,
                "failure_category": payload.get("failure_category"),
                "timeout_seconds": timeout_seconds,
                "memory_limit_mb": getattr(self.resource_policy, "memory_limit_mb", None),
                "cpu_time_limit_seconds": getattr(self.resource_policy, "cpu_time_limit_seconds", None),
                "project_workspace": str(project_workspace),
            },
        )


class ShellCommandWorker(CommandWorker):
    def __init__(
        self,
        command: str,
        timeout_policy=None,
        resource_policy=None,
        env_passthrough: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            command=command,
            backend_name="shell",
            timeout_policy=timeout_policy,
            resource_policy=resource_policy,
            env_passthrough=env_passthrough,
            extra_env=extra_env,
        )


class AgentCommandWorker(CommandWorker):
    def __init__(
        self,
        command: str,
        timeout_policy=None,
        resource_policy=None,
        env_passthrough: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            command=command,
            backend_name="agent",
            timeout_policy=timeout_policy,
            resource_policy=resource_policy,
            env_passthrough=env_passthrough,
            extra_env=extra_env,
        )


class LLMTaskWorker:
    def __init__(self, router: LLMRouter, model: str | None = None, telemetry=None) -> None:
        self.router = router
        self.model = model
        self.telemetry = telemetry

    def work(self, task: Task, run: Run, workspace_root: Path) -> WorkResult:
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        project_workspace = _prepared_project_workspace(run_dir)
        prompt = self._build_prompt(task, run)
        routed_backend = self.router.backend
        try:
            result, routed_backend = self.router.execute(
                invocation=LLMInvocation(
                    task=task, run=run, prompt=prompt, run_dir=project_workspace, model=self.model
                ),
                telemetry=self.telemetry,
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
                        "worker_outcome": "blocked",
                        "blocked": True,
                        "infrastructure_failure": True,
                        "failure_category": "llm_executor_failure",
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            return WorkResult(
                summary=f"LLM worker failed via {routed_backend} before task work completed.",
                artifacts=[
                    ("report", str(report_path), "Structured run report"),
                    ("llm_error", str(error_path), "LLM execution failure"),
                ],
                outcome="blocked",
                diagnostics={
                    "worker_backend": "llm",
                    "llm_backend": routed_backend,
                    "llm_model": self.model,
                    "error": str(exc),
                    "blocked": True,
                    "infrastructure_failure": True,
                    "failure_category": "llm_executor_failure",
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
                    "worker_outcome": payload.get("worker_outcome", "success"),
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


def build_worker_from_config(config: HarnessConfig, telemetry=None) -> WorkerBackend:
    from .resource_limits import ResourceLimitPolicy, resolve_memory_limit_mb
    from .timeout_policy import ExecutionTimeoutPolicy

    adapter_registry = build_adapter_registry(config.adapter_modules)
    timeout_policy = ExecutionTimeoutPolicy(
        telemetry,
        alpha=config.timeout_ema_alpha,
        min_seconds=config.timeout_min_seconds,
        max_seconds=config.timeout_max_seconds,
        multiplier=config.timeout_multiplier,
    )
    resource_policy = ResourceLimitPolicy(
        memory_limit_mb=resolve_memory_limit_mb(
            config.memory_limit_mb,
            backend_names=tuple(
                backend
                for backend, command in (
                    ("command", config.llm_command),
                    ("codex", config.llm_codex_command),
                    ("claude", config.llm_claude_command),
                    ("accruvia_client", config.llm_accruvia_client_command),
                )
                if command
            ),
        ) if config.worker_backend == "agent" else config.memory_limit_mb,
        cpu_time_limit_seconds=config.cpu_time_limit_seconds,
    )
    if config.worker_backend == "llm":
        return LLMTaskWorker(build_llm_router(config, telemetry=telemetry), model=config.llm_model, telemetry=telemetry)
    if config.worker_backend == "local":
        return LocalArtifactWorker(adapter_registry=adapter_registry)
    if config.worker_backend == "shell":
        if not config.worker_command:
            raise ValueError("Shell worker backend requires ACCRUVIA_WORKER_COMMAND")
        return ShellCommandWorker(
            config.worker_command,
            timeout_policy=timeout_policy,
            resource_policy=resource_policy,
            env_passthrough=config.env_passthrough,
        )
    if config.worker_backend == "agent":
        command = config.worker_command or _default_agent_worker_command()
        return AgentCommandWorker(
            command,
            timeout_policy=timeout_policy,
            resource_policy=resource_policy,
            env_passthrough=config.env_passthrough,
            extra_env={
                key: value
                for key, value in {
                    "ACCRUVIA_WORKER_LLM_BACKEND": config.llm_backend,
                    "ACCRUVIA_LLM_COMMAND": config.llm_command,
                    "ACCRUVIA_LLM_CODEX_COMMAND": config.llm_codex_command,
                    "ACCRUVIA_LLM_CLAUDE_COMMAND": config.llm_claude_command,
                    "ACCRUVIA_LLM_ACCRUVIA_CLIENT_COMMAND": config.llm_accruvia_client_command,
                }.items()
                if value
            },
        )
    return build_worker(config.worker_backend, config.worker_command)
