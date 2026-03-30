from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .atomicity import atomicity_gate, changed_files, write_atomicity_telemetry
from .llm import _coerce_subprocess_output, command_uses_file_contract, run_command_process


DEFAULT_AGENT_TEST_TIMEOUT_SECONDS = 120
DEFAULT_AGENT_TEST_STARTUP_TIMEOUT_SECONDS = 30
DEFAULT_AGENT_LLM_TIMEOUT_SECONDS = 420
DEFAULT_AGENT_COMPILE_TIMEOUT_SECONDS = 120
DEFAULT_AGENT_GIT_TIMEOUT_SECONDS = 30
DEFAULT_AGENT_PROGRESS_HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    command: list[str]
    startup_timeout_seconds: int
    execution_timeout_seconds: int


def _focused_test_command(validation_mode: str) -> list[str]:
    if validation_mode == "lightweight_repair":
        return [sys.executable, "-m", "unittest", "-v", "tests.test_workers"]
    if validation_mode == "lightweight_operator":
        return [sys.executable, "-m", "unittest", "-v", "tests.test_phase1"]
    return [
        sys.executable,
        "-m",
        "unittest",
        "-v",
        "tests.test_engine",
        "tests.test_store",
        "tests.test_validation",
        "tests.test_phase1",
    ]


def _python_test_module_from_path(path: str) -> str | None:
    normalized = path.strip().replace("\\", "/")
    if not normalized.startswith("tests/") or not normalized.endswith(".py"):
        return None
    module = normalized[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    return module if module.startswith("tests.") else None


def _task_specific_test_command(test_files: list[str], validation_mode: str) -> list[str]:
    modules: list[str] = []
    for path in test_files:
        module = _python_test_module_from_path(path)
        if module and module not in modules:
            modules.append(module)
    if modules:
        return [sys.executable, "-m", "unittest", "-v", *modules]
    return _focused_test_command(validation_mode)


def _agent_test_timeout_seconds(environ: Mapping[str, str]) -> int:
    raw_value = str(
        environ.get("ACCRUVIA_AGENT_TEST_TIMEOUT_SECONDS")
        or environ.get("ACCRUVIA_TASK_VALIDATION_TIMEOUT_SECONDS")
        or ""
    ).strip()
    if not raw_value:
        return DEFAULT_AGENT_TEST_TIMEOUT_SECONDS
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_AGENT_TEST_TIMEOUT_SECONDS
    return parsed if parsed > 0 else DEFAULT_AGENT_TEST_TIMEOUT_SECONDS


def _agent_test_startup_timeout_seconds(environ: Mapping[str, str]) -> int:
    raw_value = str(
        environ.get("ACCRUVIA_AGENT_TEST_STARTUP_TIMEOUT_SECONDS")
        or environ.get("ACCRUVIA_TASK_VALIDATION_STARTUP_TIMEOUT_SECONDS")
        or ""
    ).strip()
    if not raw_value:
        return DEFAULT_AGENT_TEST_STARTUP_TIMEOUT_SECONDS
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_AGENT_TEST_STARTUP_TIMEOUT_SECONDS
    return parsed if parsed > 0 else DEFAULT_AGENT_TEST_STARTUP_TIMEOUT_SECONDS


def _validation_policy(
    validation_mode: str,
    environ: Mapping[str, str],
    *,
    test_files: list[str] | None = None,
) -> ValidationPolicy:
    execution_timeout_seconds = _agent_test_timeout_seconds(environ)
    startup_timeout_seconds = _agent_test_startup_timeout_seconds(environ)
    command = _task_specific_test_command(test_files or [], validation_mode)
    if validation_mode == "lightweight_repair":
        return ValidationPolicy(
            command=command,
            startup_timeout_seconds=min(startup_timeout_seconds, 20),
            execution_timeout_seconds=min(execution_timeout_seconds, 45),
        )
    if validation_mode == "lightweight_operator":
        return ValidationPolicy(
            command=command,
            startup_timeout_seconds=min(startup_timeout_seconds, 20),
            execution_timeout_seconds=min(execution_timeout_seconds, 60),
        )
    return ValidationPolicy(
        command=command,
        startup_timeout_seconds=startup_timeout_seconds,
        execution_timeout_seconds=execution_timeout_seconds,
    )


def _env_timeout_seconds(environ: Mapping[str, str], key: str, default: int) -> int:
    raw_value = str(environ.get(key, "")).strip()
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_float_seconds(environ: Mapping[str, str], key: str, default: float) -> float:
    raw_value = str(environ.get(key, "")).strip()
    if not raw_value:
        return default
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _run_bounded_process(
    args: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
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
    return subprocess.CompletedProcess(args=args, returncode=process.returncode, stdout=stdout, stderr=stderr)


def _terminate_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    os.killpg(process.pid, signal.SIGKILL)
    try:
        return process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        process.kill()
        process.wait(timeout=1)
        return "", ""


def _run_validation_process(
    args: list[str],
    *,
    cwd: Path,
    startup_timeout_seconds: int,
    execution_timeout_seconds: int,
    env: Mapping[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str | None]:
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started_at = time.monotonic()
    saw_output = False
    last_stdout = ""
    last_stderr = ""
    while True:
        try:
            stdout, stderr = process.communicate(timeout=1)
            return subprocess.CompletedProcess(args=args, returncode=process.returncode, stdout=stdout, stderr=stderr), None
        except subprocess.TimeoutExpired as exc:
            last_stdout = _coerce_subprocess_output(exc.stdout)
            last_stderr = _coerce_subprocess_output(exc.stderr)
            saw_output = saw_output or bool(last_stdout.strip() or last_stderr.strip())
            elapsed = time.monotonic() - started_at
            if not saw_output and elapsed >= startup_timeout_seconds:
                stdout, stderr = _terminate_process_group(process)
                return (
                    subprocess.CompletedProcess(
                        args=args,
                        returncode=124,
                        stdout=stdout or last_stdout,
                        stderr=stderr or last_stderr,
                    ),
                    "validation_startup_timeout",
                )
            if elapsed >= execution_timeout_seconds:
                stdout, stderr = _terminate_process_group(process)
                return (
                    subprocess.CompletedProcess(
                        args=args,
                        returncode=124,
                        stdout=stdout or last_stdout,
                        stderr=stderr or last_stderr,
                    ),
                    "validation_timeout",
                )


def _workspace_contract_issues(
    workspace: Path,
    *,
    changed_files: list[str],
    test_command: list[str],
) -> list[str]:
    issues: list[str] = []
    if not workspace.exists():
        return [f"Workspace root does not exist: {workspace}"]
    if not workspace.is_dir():
        return [f"Workspace root is not a directory: {workspace}"]

    missing_targets = [path for path in changed_files if path.endswith(".py") and not (workspace / path).exists()]
    if missing_targets:
        preview = ", ".join(sorted(missing_targets)[:5])
        issues.append(f"Workspace is missing expected Python targets: {preview}")

    module_targets = [part for part in test_command if part.startswith("tests.")]
    if module_targets and not (workspace / "tests").exists():
        issues.append("Workspace is missing the tests/ directory required by validation.")

    return issues


def _validation_subprocess_env(workspace: Path, environ: Mapping[str, str]) -> dict[str, str]:
    validation_env = dict(os.environ)
    validation_env.update(environ)
    src_path = str((workspace / "src").resolve())
    existing_pythonpath = str(validation_env.get("PYTHONPATH", "")).strip()
    validation_env["PYTHONPATH"] = (
        src_path if not existing_pythonpath else src_path + os.pathsep + existing_pythonpath
    )
    return validation_env


def _first_validation_failure_line(stdout: str, stderr: str) -> str:
    combined = (stdout or "") + "\n" + (stderr or "")
    for line in combined.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:240]
    return ""


def select_worker_llm_command(environ: Mapping[str, str]) -> tuple[str, str]:
    chain = select_worker_llm_chain(environ)
    return chain[0] if chain else ("codex", "codex exec")


def select_worker_llm_chain(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """Return an ordered list of (backend, command) to try for worker execution."""
    preferred = str(environ.get("ACCRUVIA_WORKER_LLM_BACKEND", "")).strip().lower()
    commands = {
        "command": str(environ.get("ACCRUVIA_LLM_COMMAND", "")).strip(),
        "codex": str(environ.get("ACCRUVIA_LLM_CODEX_COMMAND", "")).strip(),
        "claude": str(environ.get("ACCRUVIA_LLM_CLAUDE_COMMAND", "")).strip(),
        "accruvia_client": str(environ.get("ACCRUVIA_LLM_ACCRUVIA_CLIENT_COMMAND", "")).strip(),
    }
    chain: list[tuple[str, str]] = []
    if preferred in commands and commands[preferred]:
        chain.append((preferred, commands[preferred]))
    for backend in ("codex", "claude", "command", "accruvia_client"):
        if commands[backend] and not any(b == backend for b, _ in chain):
            chain.append((backend, commands[backend]))
    return chain or [("codex", "codex exec")]


def _is_backend_unavailable(returncode: int, stdout: str, stderr: str) -> bool:
    """A backend that exits non-zero without producing useful output is unavailable.

    Real errors (syntax failures, permission issues, actual LLM responses) always
    produce output. An empty response means the backend couldn't even start work —
    credits exhausted, auth failure, service down, etc.
    """
    if returncode == 0:
        return False
    useful_output = (stdout or "").strip() + (stderr or "").strip()
    return len(useful_output) == 0


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def run_agent_worker(environ: Mapping[str, str] | None = None) -> int:
    env = dict(environ or os.environ)
    run_dir = Path(env["ACCRUVIA_RUN_DIR"]).resolve()
    workspace = Path(env["ACCRUVIA_PROJECT_WORKSPACE"]).resolve()
    task_id = env["ACCRUVIA_TASK_ID"]
    run_id = env["ACCRUVIA_RUN_ID"]
    objective = env["ACCRUVIA_TASK_OBJECTIVE"]
    summary = env.get("ACCRUVIA_RUN_SUMMARY", "")
    strategy = env.get("ACCRUVIA_TASK_STRATEGY", "default")
    validation_profile = env.get("ACCRUVIA_TASK_VALIDATION_PROFILE", "generic")
    validation_mode = env.get("ACCRUVIA_TASK_VALIDATION_MODE", "default_focused")

    run_dir.mkdir(parents=True, exist_ok=True)
    plan_path = run_dir / "plan.txt"
    report_path = run_dir / "report.json"
    stdout_path = run_dir / "codex_worker.stdout.txt"
    stderr_path = run_dir / "codex_worker.stderr.txt"
    compile_output_path = run_dir / "compile_output.txt"
    test_output_path = run_dir / "test_output.txt"
    atomicity_path = run_dir / "atomicity_telemetry.json"
    prompt_path = run_dir / "codex_worker_prompt.txt"
    metadata_path = run_dir / "codex_worker.metadata.json"
    heartbeat_path = run_dir / "worker.heartbeat.json"
    phase_path = run_dir / "phase.txt"
    llm_timeout_seconds = _env_timeout_seconds(env, "ACCRUVIA_TASK_LLM_TIMEOUT_SECONDS", DEFAULT_AGENT_LLM_TIMEOUT_SECONDS)
    compile_timeout_seconds = _env_timeout_seconds(
        env,
        "ACCRUVIA_TASK_COMPILE_TIMEOUT_SECONDS",
        DEFAULT_AGENT_COMPILE_TIMEOUT_SECONDS,
    )
    git_timeout_seconds = _env_timeout_seconds(env, "ACCRUVIA_TASK_GIT_TIMEOUT_SECONDS", DEFAULT_AGENT_GIT_TIMEOUT_SECONDS)
    progress_heartbeat_seconds = _env_float_seconds(
        env,
        "ACCRUVIA_PROGRESS_HEARTBEAT_SECONDS",
        DEFAULT_AGENT_PROGRESS_HEARTBEAT_SECONDS,
    )

    plan_path.write_text(
        "\n".join(
            [
                f"Task {task_id}",
                f"Run {run_id}",
                f"Strategy: {strategy}",
                f"Objective: {objective}",
                f"Plan summary: {summary}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    prompt_text = "\n".join(
        [
            "You are operating inside an isolated git worktree for the repository under test.",
            "",
            f"Task ID: {task_id}",
            f"Run ID: {run_id}",
            f"Objective: {objective}",
            f"Plan summary: {summary}",
            "",
            "Requirements:",
            "- Make the smallest reasonable code changes to accomplish the objective.",
            "- Work only inside the current repository checkout.",
            "- Prefer touching tests when behavior or UX changes.",
            "- Do not ask for interactive approval.",
            "- Before finishing, run a focused validation command if practical.",
            "- Print a short plain-English completion summary to stdout.",
            "",
        ]
    )
    prompt_path.write_text(prompt_text, encoding="utf-8")

    llm_chain = select_worker_llm_chain(env)
    llm_env = dict(env)
    llm_env.update(
        {
            "ACCRUVIA_RUN_DIR": str(run_dir),
            "ACCRUVIA_PROJECT_WORKSPACE": str(workspace),
            "ACCRUVIA_LLM_PROMPT_PATH": str(prompt_path),
            "ACCRUVIA_LLM_RESPONSE_PATH": str(stdout_path),
            "ACCRUVIA_LLM_METADATA_PATH": str(metadata_path),
        }
    )

    completed = None
    llm_backend = llm_chain[0][0] if llm_chain else "codex"
    chain_failures: list[str] = []
    for llm_backend, llm_command in llm_chain:
        try:
            completed = run_command_process(
                llm_command,
                cwd=workspace,
                env=llm_env,
                timeout_seconds=llm_timeout_seconds,
                stdin_text=prompt_text if not command_uses_file_contract(llm_command) else None,
                progress_path=heartbeat_path,
                progress_interval_seconds=progress_heartbeat_seconds,
                phase_path=phase_path,
                phase_name="llm_generation",
            )
            if completed.returncode == 0:
                break
            # Non-zero exit: log output and try next backend
            chain_failures.append(f"{llm_backend}: exit code {completed.returncode}")
            if _is_backend_unavailable(completed.returncode, completed.stdout, completed.stderr):
                chain_failures[-1] += " (no output — backend unavailable)"
            completed = None
        except subprocess.TimeoutExpired as exc:
            chain_failures.append(f"{llm_backend}: timed out after {llm_timeout_seconds}s")
            stdout_path.write_text(_coerce_subprocess_output(exc.stdout), encoding="utf-8")
            stderr_path.write_text(_coerce_subprocess_output(exc.stderr), encoding="utf-8")
            completed = None

    if completed is None:
        all_backends_unavailable = all("backend unavailable" in f for f in chain_failures)
        failure_cat = "llm_backends_unavailable" if all_backends_unavailable else "executor_timeout"
        report_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "objective": objective,
                    "strategy": strategy,
                    "worker_backend": "agent",
                    "llm_backend": llm_backend,
                    "validation_profile": validation_profile,
                    "validation_mode": validation_mode,
                    "worker_outcome": "blocked",
                    "blocked": True,
                    "infrastructure_failure": True,
                    "backends_unavailable": all_backends_unavailable,
                    "failure_category": failure_cat,
                    "failure_message": f"All worker backends failed: {'; '.join(chain_failures)}",
                    "timeout_seconds": llm_timeout_seconds,
                    "changed_files": [],
                    "test_files": [],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return 1

    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")

    compile_timed_out = False
    git_timed_out = False
    compile_failure_message = ""
    git_failure_message = ""
    try:
        all_changed = changed_files(workspace)
        python_files = "\n".join(path for path in all_changed if path.endswith(".py"))
    except Exception as exc:
        git_timed_out = True
        all_changed = []
        python_files = ""
        git_failure_message = f"Git metadata scan failed before validation: {exc}"
        compile_output_path.write_text(git_failure_message + "\n", encoding="utf-8")
    prior_timeout_count = 1 if "timeout" in summary.lower() else 0
    gate_result = atomicity_gate(
        workspace=workspace,
        title=env.get("ACCRUVIA_TASK_TITLE", task_id),
        objective=objective,
        strategy=strategy,
        validation_mode=validation_mode,
        attempt=int(env.get("ACCRUVIA_RUN_ATTEMPT", "1") or "1"),
        prior_timeout_count=prior_timeout_count,
    )
    write_atomicity_telemetry(atomicity_path, gate_result)
    # block_self_referential is a hard safety gate — the worker must not
    # proceed when modifying its own validation/task-selection machinery.
    if gate_result.action == "block_self_referential":
        report_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "objective": objective,
                    "strategy": strategy,
                    "worker_backend": "agent",
                    "llm_backend": llm_backend,
                    "validation_profile": validation_profile,
                    "validation_mode": validation_mode,
                    "worker_outcome": "blocked",
                    "blocked": True,
                    "failure_category": "policy_self_modification",
                    "failure_message": gate_result.rationale,
                    "changed_files": all_changed,
                    "test_files": [path for path in all_changed if path.startswith("tests/")],
                    "atomicity_gate": {
                        "score": gate_result.score,
                        "flags": gate_result.flags,
                        "action": gate_result.action,
                        "rationale": gate_result.rationale,
                    },
                    "atomicity_telemetry_path": str(atomicity_path),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return 1
    # narrow_scope: the gate detected elevated risk but the candidate can still
    # proceed to validation. The scope metadata is included so the retry advisor
    # can narrow the next attempt if validation fails.

    # LLM succeeded and atomicity gate passed — emit candidate report for separate validation.
    llm_failed = completed.returncode != 0
    if llm_failed:
        summary_text = _first_nonempty_line(completed.stdout)
        failure_message = _first_nonempty_line(completed.stderr) or _first_nonempty_line(completed.stdout)
        report_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "objective": objective,
                    "strategy": strategy,
                    "worker_backend": "agent",
                    "llm_backend": llm_backend,
                    "validation_profile": validation_profile,
                    "validation_mode": validation_mode,
                    "worker_outcome": "blocked",
                    "blocked": True,
                    "infrastructure_failure": True,
                    "failure_category": "executor_process_failure",
                    "failure_message": failure_message or f"{llm_backend} worker exited non-zero",
                    "llm_returncode": completed.returncode,
                    "changed_files": all_changed,
                    "test_files": [],
                    "summary": summary_text,
                    "atomicity_gate": {
                        "score": gate_result.score,
                        "flags": gate_result.flags,
                        "action": gate_result.action,
                        "rationale": gate_result.rationale,
                    },
                    "atomicity_telemetry_path": str(atomicity_path),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return 1

    test_files = [
        path for path in all_changed if "/test" in path or path.startswith("tests/") or path.endswith("_test.py") or path.endswith(".test.js")
    ]
    summary_text = _first_nonempty_line(completed.stdout)
    if not summary_text and stdout_path.exists():
        summary_text = _first_nonempty_line(stdout_path.read_text(encoding="utf-8", errors="replace"))

    candidate_payload = {
        "task_id": task_id,
        "run_id": run_id,
        "objective": objective,
        "strategy": strategy,
        "worker_backend": "agent",
        "llm_backend": llm_backend,
        "validation_profile": validation_profile,
        "validation_mode": validation_mode,
        "worker_outcome": "candidate",
        "changed_files": all_changed,
        "test_files": test_files,
        "summary": summary_text,
        "command": llm_command,
        "atomicity_gate": {
            "score": gate_result.score,
            "flags": gate_result.flags,
            "action": gate_result.action,
            "rationale": gate_result.rationale,
        },
        "atomicity_telemetry_path": str(atomicity_path),
        "effective_validation_mode": gate_result.effective_validation_mode,
    }
    report_path.write_text(json.dumps(candidate_payload, indent=2, sort_keys=True), encoding="utf-8")

    # Candidate produced. Validation runs as a separate step orchestrated by run_service.
    return 0


def run_validation(environ: Mapping[str, str] | None = None) -> int:
    """Run compile and test validation on an existing candidate report.

    Reads report.json from run_dir, runs py_compile and pytest, then updates
    the report with compile_check and test_check results.  Can be called as a
    standalone subprocess or from within run_agent_worker.
    """
    env = dict(environ or os.environ)
    run_dir = Path(env["ACCRUVIA_RUN_DIR"]).resolve()
    workspace = Path(env["ACCRUVIA_PROJECT_WORKSPACE"]).resolve()
    validation_mode = env.get("ACCRUVIA_TASK_VALIDATION_MODE", "default_focused")
    compile_timeout_seconds = _env_timeout_seconds(
        env,
        "ACCRUVIA_TASK_COMPILE_TIMEOUT_SECONDS",
        DEFAULT_AGENT_COMPILE_TIMEOUT_SECONDS,
    )

    report_path = run_dir / "report.json"
    compile_output_path = run_dir / "compile_output.txt"
    test_output_path = run_dir / "test_output.txt"

    # Read existing report
    payload: dict[str, object] = {}
    if report_path.exists():
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}

    all_changed = payload.get("changed_files", [])
    if not isinstance(all_changed, list):
        all_changed = []
    effective_validation_mode = str(payload.get("effective_validation_mode") or validation_mode)
    test_files = [path for path in payload.get("test_files", []) if isinstance(path, str)]
    validation_policy = _validation_policy(
        effective_validation_mode,
        env,
        test_files=test_files,
    )
    test_command = validation_policy.command
    test_timeout_seconds = validation_policy.execution_timeout_seconds
    test_startup_timeout_seconds = validation_policy.startup_timeout_seconds
    validation_env = _validation_subprocess_env(workspace, env)
    _validation_start = time.monotonic()

    workspace_contract_issues = _workspace_contract_issues(
        workspace,
        changed_files=[path for path in all_changed if isinstance(path, str)],
        test_command=test_command,
    )
    if workspace_contract_issues:
        failure_message = "Validation workspace contract failed: " + " ".join(workspace_contract_issues)
        compile_output_path.write_text(failure_message + "\n", encoding="utf-8")
        test_output_path.write_text(failure_message + "\n", encoding="utf-8")
        payload.update(
            {
                "worker_outcome": "failed",
                "compile_check": {
                    "passed": False,
                    "targets": [path for path in all_changed if path.endswith(".py")],
                    "mode": "py_compile",
                    "output_path": str(compile_output_path),
                    "timeout_seconds": compile_timeout_seconds,
                    "timed_out": False,
                },
                "test_check": {
                    "passed": False,
                    "framework": "unittest",
                    "command": test_command,
                    "output_path": str(test_output_path),
                    "selection": effective_validation_mode,
                    "timeout_seconds": test_timeout_seconds,
                    "startup_timeout_seconds": test_startup_timeout_seconds,
                    "timed_out": False,
                },
                "validation_elapsed_seconds": round(time.monotonic() - _validation_start, 2),
                "failure_category": "workspace_contract_failure",
                "failure_message": failure_message,
                "workspace_contract_failure": True,
                "workspace_contract_issues": workspace_contract_issues,
                "infrastructure_failure": True,
            }
        )
        report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return 1

    compile_timed_out = False
    git_timed_out = False
    compile_failure_message = ""
    git_failure_message = ""
    python_files = "\n".join(path for path in all_changed if path.endswith(".py"))

    if python_files:
        try:
            compile_completed = _run_bounded_process(
                [sys.executable, "-m", "py_compile", *python_files.splitlines()],
                cwd=workspace,
                timeout_seconds=compile_timeout_seconds,
                env=validation_env,
            )
            compile_output_path.write_text(
                (compile_completed.stdout or "") + (compile_completed.stderr or ""),
                encoding="utf-8",
            )
            compile_rc = compile_completed.returncode
        except subprocess.TimeoutExpired as exc:
            compile_timed_out = True
            compile_rc = 124
            compile_failure_message = (
                f"Compile validation exceeded {compile_timeout_seconds} seconds and was terminated."
            )
            compile_output_path.write_text(
                compile_failure_message + "\n" + _coerce_subprocess_output(exc.stdout) + _coerce_subprocess_output(exc.stderr),
                encoding="utf-8",
            )
    else:
        compile_output_path.write_text("No Python files changed.\n", encoding="utf-8")
        compile_rc = 0

    test_completed, test_timeout_category = _run_validation_process(
        test_command,
        cwd=workspace,
        startup_timeout_seconds=test_startup_timeout_seconds,
        execution_timeout_seconds=test_timeout_seconds,
        env=validation_env,
    )
    test_timed_out = test_timeout_category is not None
    timeout_summary = ""
    if test_timeout_category == "validation_startup_timeout":
        timeout_summary = (
            f"Focused unit-test validation produced no output within the {test_startup_timeout_seconds}s startup ceiling and was terminated.\n"
        )
    elif test_timeout_category == "validation_timeout":
        timeout_summary = (
            f"Focused unit-test validation hit the {test_timeout_seconds}s execution ceiling and was terminated.\n"
        )
    test_output_path.write_text(
        timeout_summary + (test_completed.stdout or "") + (test_completed.stderr or ""),
        encoding="utf-8",
    )

    _validation_elapsed = time.monotonic() - _validation_start

    # Determine final outcome
    worker_outcome = payload.get("worker_outcome", "candidate")
    if compile_rc != 0 or test_completed.returncode != 0:
        worker_outcome = "failed"
    elif worker_outcome == "candidate":
        worker_outcome = "success"

    payload.update({
        "worker_outcome": worker_outcome,
        "compile_check": {
            "passed": compile_rc == 0,
            "targets": [path for path in all_changed if path.endswith(".py")],
            "mode": "py_compile",
            "output_path": str(compile_output_path),
            "timeout_seconds": compile_timeout_seconds,
            "timed_out": compile_timed_out,
        },
        "test_check": {
            "passed": test_completed.returncode == 0,
            "framework": "unittest",
            "command": test_command,
            "output_path": str(test_output_path),
            "selection": effective_validation_mode,
            "timeout_seconds": test_timeout_seconds,
            "startup_timeout_seconds": test_startup_timeout_seconds,
            "timed_out": test_timed_out,
        },
        "validation_elapsed_seconds": round(_validation_elapsed, 2),
    })
    if test_timeout_category == "validation_startup_timeout":
        payload.update(
            {
                "failure_category": "validation_startup_timeout",
                "failure_message": (
                    f"Focused unit-test validation produced no output within {test_startup_timeout_seconds} seconds and was terminated."
                ),
            }
        )
    elif test_timed_out:
        payload.update(
            {
                "failure_category": "validation_timeout",
                "failure_message": f"Focused unit-test validation exceeded {test_timeout_seconds} seconds and was terminated.",
            }
        )
    elif compile_timed_out:
        payload.update(
            {
                "failure_category": "compile_timeout",
                "failure_message": compile_failure_message,
            }
        )
    elif compile_rc != 0:
        payload.update(
            {
                "failure_category": "compile_failure",
                "failure_message": _first_validation_failure_line(
                    compile_output_path.read_text(encoding="utf-8"),
                    "",
                )
                or "Compile validation failed.",
            }
        )
    elif test_completed.returncode != 0:
        payload.update(
            {
                "failure_category": "validation_failure",
                "failure_message": _first_validation_failure_line(
                    test_completed.stdout or "",
                    test_completed.stderr or "",
                )
                or "Focused unit-test validation failed.",
            }
        )

    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    if compile_rc != 0 or test_completed.returncode != 0:
        return 1
    return 0
