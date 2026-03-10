from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import HarnessConfig
from .domain import Run, Task


@dataclass(slots=True)
class LLMInvocation:
    task: Task
    run: Run
    prompt: str
    run_dir: Path
    model: str | None = None


@dataclass(slots=True)
class LLMExecutionResult:
    backend: str
    response_text: str
    prompt_path: Path
    response_path: Path
    diagnostics: dict[str, object]


class LLMExecutor(Protocol):
    backend_name: str

    def execute(self, invocation: LLMInvocation) -> LLMExecutionResult: ...


class LLMExecutionError(RuntimeError):
    """Raised when an LLM executor cannot complete a requested invocation."""


class CommandLLMExecutor:
    def __init__(self, backend_name: str, command: str) -> None:
        self.backend_name = backend_name
        self.command = command

    def execute(self, invocation: LLMInvocation) -> LLMExecutionResult:
        invocation.run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = invocation.run_dir / "llm_prompt.txt"
        response_path = invocation.run_dir / "llm_response.md"
        stdout_path = invocation.run_dir / "llm.stdout.txt"
        stderr_path = invocation.run_dir / "llm.stderr.txt"
        prompt_path.write_text(invocation.prompt, encoding="utf-8")

        env = {
            **os.environ,
            "ACCRUVIA_TASK_ID": invocation.task.id,
            "ACCRUVIA_RUN_ID": invocation.run.id,
            "ACCRUVIA_TASK_OBJECTIVE": invocation.task.objective,
            "ACCRUVIA_TASK_TITLE": invocation.task.title,
            "ACCRUVIA_TASK_STRATEGY": invocation.task.strategy,
            "ACCRUVIA_RUN_DIR": str(invocation.run_dir),
            "ACCRUVIA_LLM_PROMPT_PATH": str(prompt_path),
            "ACCRUVIA_LLM_RESPONSE_PATH": str(response_path),
        }
        if invocation.model:
            env["ACCRUVIA_LLM_MODEL"] = invocation.model

        completed = subprocess.run(
            self.command,
            shell=True,
            check=False,
            cwd=invocation.run_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")

        if response_path.exists():
            response_text = response_path.read_text(encoding="utf-8")
        else:
            response_text = completed.stdout
            response_path.write_text(response_text, encoding="utf-8")

        if completed.returncode != 0:
            raise LLMExecutionError(
                f"{self.backend_name} executor failed with return code {completed.returncode}"
            )

        return LLMExecutionResult(
            backend=self.backend_name,
            response_text=response_text,
            prompt_path=prompt_path,
            response_path=response_path,
            diagnostics={
                "backend": self.backend_name,
                "command": self.command,
                "returncode": completed.returncode,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            },
        )


class LLMRouter:
    def __init__(self, backend: str, executors: dict[str, LLMExecutor]) -> None:
        self.backend = backend
        self.executors = executors

    def resolve(self) -> tuple[LLMExecutor, str]:
        backend = self.backend
        if backend == "auto":
            backend = self._auto_backend()
        executor = self.executors.get(backend)
        if executor is None:
            available = ", ".join(sorted(self.executors))
            raise ValueError(f"Unsupported LLM backend '{backend}'. Available: {available}")
        return executor, backend

    def _auto_backend(self) -> str:
        if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
            if "accruvia_client" in self.executors:
                return "accruvia_client"
            if "command" in self.executors:
                return "command"
        for candidate in ("codex", "claude", "accruvia_client", "command"):
            if candidate in self.executors:
                return candidate
        raise ValueError("No LLM executors are configured for ACCRUVIA_LLM_BACKEND=auto")


def build_llm_router(config: HarnessConfig) -> LLMRouter:
    executors: dict[str, LLMExecutor] = {}
    if config.llm_command:
        executors["command"] = CommandLLMExecutor("command", config.llm_command)
    if config.llm_codex_command:
        executors["codex"] = CommandLLMExecutor("codex", config.llm_codex_command)
    if config.llm_claude_command:
        executors["claude"] = CommandLLMExecutor("claude", config.llm_claude_command)
    if config.llm_accruvia_client_command:
        executors["accruvia_client"] = CommandLLMExecutor(
            "accruvia_client", config.llm_accruvia_client_command
        )
    return LLMRouter(config.llm_backend, executors)


def parse_affirmation_response(text: str) -> tuple[bool, str]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("LLM affirmation response was empty")
    try:
        import json

        payload = json.loads(stripped)
        if isinstance(payload, dict):
            approved = payload.get("approved")
            rationale = str(payload.get("rationale") or payload.get("summary") or stripped)
            if isinstance(approved, bool):
                return approved, rationale
    except Exception:
        pass

    lowered = stripped.lower()
    first_line, *rest = stripped.splitlines()
    rationale = "\n".join(rest).strip() or first_line.strip()
    if first_line.strip().upper().startswith("APPROVE") or " approve" in f" {lowered[:240]}":
        return True, rationale
    if first_line.strip().upper().startswith("REJECT") or " reject" in f" {lowered[:240]}":
        return False, rationale
    if "should be promoted" in lowered and any(token in lowered for token in ("yes", "approve", "promote it")):
        return True, rationale
    if "should not be promoted" in lowered or any(
        token in lowered for token in ("do not promote", "not ready to promote", "reject this candidate")
    ):
        return False, rationale
    raise ValueError("Unable to infer LLM affirmation decision from response text")
