from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

from .config import HarnessConfig, default_config_path, write_persisted_config


@dataclass(slots=True)
class LLMCommandCandidate:
    backend: str
    label: str
    executable: str
    command: str
    available: bool
    resolved_path: str | None


def detect_llm_command_candidates(search_path: str | None = None) -> list[LLMCommandCandidate]:
    path_value = search_path if search_path is not None else os.environ.get("PATH")
    candidates = [
        (
            "codex",
            "Codex CLI",
            "codex",
            "codex exec",
        ),
        (
            "claude",
            "Claude CLI",
            "claude",
            "claude",
        ),
    ]
    discovered: list[LLMCommandCandidate] = []
    for backend, label, executable, command in candidates:
        resolved = shutil.which(executable, path=path_value)
        discovered.append(
            LLMCommandCandidate(
                backend=backend,
                label=label,
                executable=executable,
                command=command,
                available=resolved is not None,
                resolved_path=resolved,
            )
        )
    return discovered


def command_executable_status(command: str | None, search_path: str | None = None) -> dict[str, object]:
    if not command or not command.strip():
        return {"configured": False, "available": False, "executable": None, "resolved_path": None}
    try:
        executable = shlex.split(command)[0]
    except ValueError:
        executable = command.strip().split()[0]
    resolved = shutil.which(executable, path=search_path if search_path is not None else os.environ.get("PATH"))
    return {
        "configured": True,
        "available": resolved is not None,
        "executable": executable,
        "resolved_path": resolved,
    }


def probe_llm_command(command: str, *, timeout_seconds: int = 20) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        prompt_path = root / "prompt.txt"
        response_path = root / "response.txt"
        prompt_path.write_text("Reply with a short confirmation that setup works.\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "ACCRUVIA_LLM_PROMPT_PATH": str(prompt_path),
                "ACCRUVIA_LLM_RESPONSE_PATH": str(response_path),
                "ACCRUVIA_LLM_METADATA_PATH": str(root / "metadata.json"),
            }
        )
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=root,
            stdin=subprocess.PIPE
            if "ACCRUVIA_LLM_PROMPT_PATH" not in command and "ACCRUVIA_LLM_RESPONSE_PATH" not in command
            else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(
                input=prompt_path.read_text(encoding="utf-8")
                if "ACCRUVIA_LLM_PROMPT_PATH" not in command and "ACCRUVIA_LLM_RESPONSE_PATH" not in command
                else None,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
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
            return {
                "ok": False,
                "message": f"The command did not finish within {timeout_seconds} seconds.",
            }
        completed = subprocess.CompletedProcess(
            args=command,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        response_text = response_path.read_text(encoding="utf-8").strip() if response_path.exists() else completed.stdout.strip()
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or "The command exited non-zero."
            return {
                "ok": False,
                "message": stderr,
            }
        if not response_text:
            return {
                "ok": False,
                "message": "The command ran but did not produce a response.",
            }
        first_line = response_text.splitlines()[0].strip()
        return {
            "ok": True,
            "message": "The command produced a response.",
            "response_preview": first_line[:120],
        }


def doctor_report(config: HarnessConfig, *, config_path: str | Path | None = None) -> dict[str, object]:
    persisted_path = Path(config_path) if config_path is not None else default_config_path()
    candidates = detect_llm_command_candidates()
    configured = {
        "command": command_executable_status(config.llm_command),
        "codex": command_executable_status(config.llm_codex_command),
        "claude": command_executable_status(config.llm_claude_command),
        "accruvia_client": command_executable_status(config.llm_accruvia_client_command),
    }
    configured_executors = [name for name, details in configured.items() if details["configured"]]
    available_executors = [name for name, details in configured.items() if details["available"]]
    issues: list[str] = []
    recommendations: list[str] = []
    if not configured_executors:
        issues.append("No LLM executor is configured.")
        recommendations.append("Run `accruvia-harness setup` or `accruvia-harness configure-llm`.")
    elif config.llm_backend == "auto" and not available_executors:
        issues.append("LLM backend is auto but no configured executor is available on PATH.")
    elif config.llm_backend != "auto" and config.llm_backend not in configured_executors:
        issues.append(f"Preferred backend `{config.llm_backend}` is not configured.")
    elif config.llm_backend != "auto" and config.llm_backend in configured and not configured[config.llm_backend]["available"]:
        issues.append(f"Preferred backend `{config.llm_backend}` is configured but not available on PATH.")
    if config.llm_backend == "auto" and available_executors:
        selected_backend = next(name for name in ("codex", "claude", "accruvia_client", "command") if name in available_executors)
    elif config.llm_backend in configured_executors:
        selected_backend = config.llm_backend
    else:
        selected_backend = None
    if selected_backend is None:
        recommendations.append("Configure a working LLM command before enabling heartbeats or read-only explanations.")
    inspection_ready = config.db_path.exists()
    task_execution_ready = inspection_ready
    heartbeats_ready = selected_backend is not None
    autonomous_ready = task_execution_ready and heartbeats_ready
    return {
        "prototype": {
            "stage": "prototype",
            "warning": "Use smoke tests and one-shot commands before trusting long-running autonomy.",
            "state_root": str(config.db_path.parent),
        },
        "config_file": {
            "path": str(persisted_path),
            "exists": persisted_path.exists(),
        },
        "harness_home": str(config.db_path.parent),
        "database": {
            "path": str(config.db_path),
            "exists": config.db_path.exists(),
        },
        "llm": {
            "backend": config.llm_backend,
            "selected_backend": selected_backend,
            "configured_executors": configured_executors,
            "available_executors": available_executors,
            "executors": configured,
            "detected_candidates": [
                {
                    "backend": item.backend,
                    "label": item.label,
                    "command": item.command,
                    "available": item.available,
                    "resolved_path": item.resolved_path,
                }
                for item in candidates
            ],
        },
        "readiness": {
            "inspection_ready": inspection_ready,
            "task_execution_ready": task_execution_ready,
            "heartbeats_ready": heartbeats_ready,
            "autonomous_ready": autonomous_ready,
        },
        "heartbeats_ready": heartbeats_ready,
        "issues": issues,
        "recommendations": recommendations,
        "next_steps": [
            "Run `./bin/accruvia-harness init-db` if the database is missing.",
            "Run `./bin/accruvia-harness smoke-test` before enabling autonomous heartbeats.",
            "Use `./bin/accruvia-harness supervise --one-shot` before long-running watch mode.",
        ],
    }


def persist_config(config: HarnessConfig, *, config_path: str | Path | None = None) -> Path:
    path = Path(config_path) if config_path is not None else default_config_path(config.db_path.parent)
    return write_persisted_config(path, config.persisted_payload())


def prompt_text(prompt: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    sys.stderr.write(f"{prompt}{suffix}: ")
    sys.stderr.flush()
    value = sys.stdin.readline()
    if value == "":
        return default or ""
    stripped = value.strip()
    if not stripped and default is not None:
        return default
    return stripped
