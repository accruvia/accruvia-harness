from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import HarnessConfig
from .domain import Run, Task
from .subprocess_env import build_subprocess_env


@dataclass(slots=True)
class LLMInvocation:
    task: Task
    run: Run
    prompt: str
    run_dir: Path
    model: str | None = None
    timeout_seconds_override: int | None = None


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


def _coerce_metric_number(value: object) -> float:
    if value in (None, "", False):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _coerce_subprocess_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


class CommandLLMExecutor:
    def __init__(
        self,
        backend_name: str,
        command: str,
        timeout_policy=None,
        resource_policy=None,
        telemetry=None,
        env_passthrough: tuple[str, ...] = (),
    ) -> None:
        self.backend_name = backend_name
        self.command = command
        self.timeout_policy = timeout_policy
        self.resource_policy = resource_policy
        self.telemetry = telemetry
        self.env_passthrough = env_passthrough

    def execute(self, invocation: LLMInvocation) -> LLMExecutionResult:
        invocation.run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = invocation.run_dir / "llm_prompt.txt"
        response_path = invocation.run_dir / "llm_response.md"
        metadata_path = invocation.run_dir / "llm_metadata.json"
        stdout_path = invocation.run_dir / "llm.stdout.txt"
        stderr_path = invocation.run_dir / "llm.stderr.txt"
        prompt_path.write_text(invocation.prompt, encoding="utf-8")

        env = build_subprocess_env(
            {
            "ACCRUVIA_TASK_ID": invocation.task.id,
            "ACCRUVIA_RUN_ID": invocation.run.id,
            "ACCRUVIA_TASK_OBJECTIVE": invocation.task.objective,
            "ACCRUVIA_TASK_TITLE": invocation.task.title,
            "ACCRUVIA_TASK_STRATEGY": invocation.task.strategy,
            "ACCRUVIA_RUN_DIR": str(invocation.run_dir),
            "ACCRUVIA_LLM_PROMPT_PATH": str(prompt_path),
            "ACCRUVIA_LLM_RESPONSE_PATH": str(response_path),
            "ACCRUVIA_LLM_METADATA_PATH": str(metadata_path),
            },
            passthrough=self.env_passthrough,
        )
        if invocation.model:
            env["ACCRUVIA_LLM_MODEL"] = invocation.model

        timeout_seconds = invocation.timeout_seconds_override
        if timeout_seconds is None and self.timeout_policy is not None:
            timeout_seconds = self.timeout_policy.timeout_seconds(
                invocation.task.validation_profile, self.backend_name
            )
        try:
            if self.telemetry is not None:
                with self.telemetry.timed(
                    "llm_execute",
                    task_id=invocation.task.id,
                    run_id=invocation.run.id,
                    llm_backend=self.backend_name,
                    validation_profile=invocation.task.validation_profile,
                ):
                    completed = self._run_command(
                        cwd=invocation.run_dir,
                        env=env,
                        timeout_seconds=timeout_seconds,
                    )
            else:
                completed = self._run_command(
                    cwd=invocation.run_dir,
                    env=env,
                    timeout_seconds=timeout_seconds,
                )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(_coerce_subprocess_output(exc.stdout), encoding="utf-8")
            stderr_path.write_text(_coerce_subprocess_output(exc.stderr), encoding="utf-8")
            raise LLMExecutionError(
                f"{self.backend_name} executor timed out after {timeout_seconds} seconds"
            ) from exc
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")

        if response_path.exists():
            response_text = response_path.read_text(encoding="utf-8")
        else:
            response_text = completed.stdout
            response_path.write_text(response_text, encoding="utf-8")

        metadata: dict[str, object] = {}
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}

        if completed.returncode != 0:
            raise LLMExecutionError(
                f"{self.backend_name} executor failed with return code {completed.returncode}"
            )

        token_metrics = {
            "llm_cost_usd": _coerce_metric_number(metadata.get("cost_usd", 0.0)),
            "llm_prompt_tokens": _coerce_metric_number(metadata.get("prompt_tokens", 0.0)),
            "llm_completion_tokens": _coerce_metric_number(metadata.get("completion_tokens", 0.0)),
            "llm_total_tokens": _coerce_metric_number(metadata.get("total_tokens", 0.0)),
            "llm_latency_ms": _coerce_metric_number(metadata.get("latency_ms", 0.0)),
        }
        if self.telemetry is not None:
            for metric_name, metric_value in token_metrics.items():
                if metric_value <= 0:
                    continue
                self.telemetry.metric(
                    metric_name,
                    metric_value,
                    metric_type="histogram" if metric_name.endswith(("_usd", "_ms")) else "counter",
                    task_id=invocation.task.id,
                    run_id=invocation.run.id,
                    llm_backend=self.backend_name,
                    model=metadata.get("model") or invocation.model,
                    validation_profile=invocation.task.validation_profile,
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
                "metadata_path": str(metadata_path),
                "timeout_seconds": timeout_seconds,
                "memory_limit_mb": getattr(self.resource_policy, "memory_limit_mb", None),
                "cpu_time_limit_seconds": getattr(self.resource_policy, "cpu_time_limit_seconds", None),
                **metadata,
            },
        )

    def _run_command(
        self,
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int | None,
    ) -> subprocess.CompletedProcess[str]:
        preexec = self.resource_policy.preexec_fn() if self.resource_policy is not None else None
        process = subprocess.Popen(
            self.command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=preexec is None,
            preexec_fn=preexec,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            try:
                stdout, stderr = process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                process.kill()
                process.wait(timeout=1)
                stdout = ""
                stderr = ""
            exc.stdout = stdout
            exc.stderr = stderr
            raise
        return subprocess.CompletedProcess(
            args=self.command,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
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

    def resolve_chain(self) -> list[tuple[LLMExecutor, str]]:
        selected = self._auto_backend() if self.backend == "auto" else self.backend
        ordered: list[str] = []
        if selected in self.executors:
            ordered.append(selected)
        for candidate in ("codex", "claude", "accruvia_client", "command"):
            if candidate in self.executors and candidate not in ordered:
                ordered.append(candidate)
        if not ordered:
            available = ", ".join(sorted(self.executors))
            raise ValueError(f"Unsupported LLM backend '{selected}'. Available: {available}")
        return [(self.executors[name], name) for name in ordered]

    def execute(self, invocation: LLMInvocation, telemetry=None) -> tuple[LLMExecutionResult, str]:
        failures: list[dict[str, str]] = []
        for executor, backend in self.resolve_chain():
            try:
                return executor.execute(invocation), backend
            except LLMExecutionError as exc:
                failures.append({"backend": backend, "error": str(exc)})
                if telemetry is not None:
                    telemetry.warn(
                        "llm_executor_failure",
                        str(exc),
                        backend=backend,
                        task_id=invocation.task.id,
                        run_id=invocation.run.id,
                    )
        details = "; ".join(f"{item['backend']}: {item['error']}" for item in failures)
        raise LLMExecutionError(f"All configured LLM executors failed. {details}")

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


def build_llm_router(config: HarnessConfig, telemetry=None) -> LLMRouter:
    from .resource_limits import ResourceLimitPolicy
    from .timeout_policy import ExecutionTimeoutPolicy

    llm_memory_limit_mb = max(config.memory_limit_mb, 4096)
    timeout_policy = ExecutionTimeoutPolicy(
        telemetry,
        alpha=config.timeout_ema_alpha,
        min_seconds=config.timeout_min_seconds,
        max_seconds=config.timeout_max_seconds,
        multiplier=config.timeout_multiplier,
    )
    resource_policy = ResourceLimitPolicy(
        memory_limit_mb=llm_memory_limit_mb,
        cpu_time_limit_seconds=config.cpu_time_limit_seconds,
    )
    executors: dict[str, LLMExecutor] = {}
    if config.llm_command:
        executors["command"] = CommandLLMExecutor(
            "command",
            config.llm_command,
            timeout_policy=timeout_policy,
            resource_policy=resource_policy,
            telemetry=telemetry,
            env_passthrough=config.env_passthrough,
        )
    if config.llm_codex_command:
        executors["codex"] = CommandLLMExecutor(
            "codex",
            config.llm_codex_command,
            timeout_policy=timeout_policy,
            resource_policy=resource_policy,
            telemetry=telemetry,
            env_passthrough=config.env_passthrough,
        )
    if config.llm_claude_command:
        executors["claude"] = CommandLLMExecutor(
            "claude",
            config.llm_claude_command,
            timeout_policy=timeout_policy,
            resource_policy=resource_policy,
            telemetry=telemetry,
            env_passthrough=config.env_passthrough,
        )
    if config.llm_accruvia_client_command:
        executors["accruvia_client"] = CommandLLMExecutor(
            "accruvia_client",
            config.llm_accruvia_client_command,
            timeout_policy=timeout_policy,
            resource_policy=resource_policy,
            telemetry=telemetry,
            env_passthrough=config.env_passthrough,
        )
    return LLMRouter(config.llm_backend, executors)


def parse_affirmation_response(text: str) -> tuple[bool, str]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("LLM affirmation response was empty")
    for candidate in _candidate_json_payloads(stripped):
        payload = _parse_json_object(candidate)
        if payload is None:
            continue
        approved, rationale = _decision_from_mapping(payload, fallback=stripped)
        if approved is not None:
            return approved, rationale

    lowered = stripped.lower()
    first_line, *rest = stripped.splitlines()
    rationale = "\n".join(rest).strip() or first_line.strip()
    structured_match = _structured_text_decision(stripped)
    if structured_match is not None:
        return structured_match, rationale
    if first_line.strip().upper().startswith("APPROVE"):
        return True, rationale
    if first_line.strip().upper().startswith("REJECT"):
        return False, rationale
    if "should be promoted" in lowered and any(token in lowered for token in ("yes", "approve", "promote it")):
        return True, rationale
    if "should not be promoted" in lowered or any(
        token in lowered for token in ("do not promote", "not ready to promote", "reject this candidate")
    ):
        return False, rationale
    raise ValueError("Unable to infer LLM affirmation decision from response text")


def _candidate_json_payloads(text: str) -> list[str]:
    candidates = [text]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates.extend(fenced)
    inline = re.findall(r"(\{.*\})", text, flags=re.DOTALL)
    candidates.extend(inline[:1])
    return candidates


def _parse_json_object(candidate: str) -> dict[str, object] | None:
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _decision_from_mapping(payload: dict[str, object], fallback: str) -> tuple[bool, str] | tuple[None, str]:
    approved = payload.get("approved")
    if isinstance(approved, bool):
        rationale = str(payload.get("rationale") or payload.get("summary") or fallback)
        return approved, rationale

    for key in ("decision", "verdict", "status", "recommendation"):
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        parsed = _normalize_decision_word(value)
        if parsed is not None:
            rationale = str(payload.get("rationale") or payload.get("summary") or fallback)
            return parsed, rationale
    return None, fallback


def _structured_text_decision(text: str) -> bool | None:
    for line in text.splitlines():
        match = re.match(
            r"^\s*(decision|verdict|approved|status|recommendation)\s*[:=-]\s*(.+?)\s*$",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        parsed = _normalize_decision_word(match.group(2))
        if parsed is not None:
            return parsed
    return None


def _normalize_decision_word(value: str) -> bool | None:
    lowered = value.strip().lower()
    positive = {"approve", "approved", "true", "yes", "promote", "promoted"}
    negative = {"reject", "rejected", "false", "no", "deny", "blocked"}
    if lowered in positive:
        return True
    if lowered in negative:
        return False
    return None
