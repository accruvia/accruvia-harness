from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Mapping

from .llm import _coerce_subprocess_output, command_uses_file_contract, run_command_process


DEFAULT_AGENT_TEST_TIMEOUT_SECONDS = 300
DEFAULT_AGENT_LLM_TIMEOUT_SECONDS = 420
DEFAULT_AGENT_COMPILE_TIMEOUT_SECONDS = 120
DEFAULT_AGENT_GIT_TIMEOUT_SECONDS = 30
LIGHTWEIGHT_REPAIR_STRATEGIES = frozenset({"executor_repair", "timeout_decomposition", "bounded_unblocker"})


def _focused_test_command(strategy: str) -> list[str]:
    if strategy in LIGHTWEIGHT_REPAIR_STRATEGIES:
        return ["python3", "-m", "unittest", "tests.test_workers"]
    return [
        "python3",
        "-m",
        "unittest",
        "tests.test_cli",
        "tests.test_phase1",
        "tests.test_supervisor",
        "tests.test_observer",
    ]


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


def _env_timeout_seconds(environ: Mapping[str, str], key: str, default: int) -> int:
    raw_value = str(environ.get(key, "")).strip()
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _run_bounded_process(
    args: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        args,
        cwd=cwd,
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


def select_worker_llm_command(environ: Mapping[str, str]) -> tuple[str, str]:
    preferred = str(environ.get("ACCRUVIA_WORKER_LLM_BACKEND", "")).strip().lower()
    commands = {
        "command": str(environ.get("ACCRUVIA_LLM_COMMAND", "")).strip(),
        "codex": str(environ.get("ACCRUVIA_LLM_CODEX_COMMAND", "")).strip(),
        "claude": str(environ.get("ACCRUVIA_LLM_CLAUDE_COMMAND", "")).strip(),
        "accruvia_client": str(environ.get("ACCRUVIA_LLM_ACCRUVIA_CLIENT_COMMAND", "")).strip(),
    }
    if preferred in commands and commands[preferred]:
        return preferred, commands[preferred]
    for backend in ("codex", "claude", "command", "accruvia_client"):
        if commands[backend]:
            return backend, commands[backend]
    return "codex", "codex exec"


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

    run_dir.mkdir(parents=True, exist_ok=True)
    plan_path = run_dir / "plan.txt"
    report_path = run_dir / "report.json"
    stdout_path = run_dir / "codex_worker.stdout.txt"
    stderr_path = run_dir / "codex_worker.stderr.txt"
    compile_output_path = run_dir / "compile_output.txt"
    test_output_path = run_dir / "test_output.txt"
    prompt_path = run_dir / "codex_worker_prompt.txt"
    metadata_path = run_dir / "codex_worker.metadata.json"
    test_timeout_seconds = _agent_test_timeout_seconds(env)
    llm_timeout_seconds = _env_timeout_seconds(env, "ACCRUVIA_TASK_LLM_TIMEOUT_SECONDS", DEFAULT_AGENT_LLM_TIMEOUT_SECONDS)
    compile_timeout_seconds = _env_timeout_seconds(
        env,
        "ACCRUVIA_TASK_COMPILE_TIMEOUT_SECONDS",
        DEFAULT_AGENT_COMPILE_TIMEOUT_SECONDS,
    )
    git_timeout_seconds = _env_timeout_seconds(env, "ACCRUVIA_TASK_GIT_TIMEOUT_SECONDS", DEFAULT_AGENT_GIT_TIMEOUT_SECONDS)

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

    llm_backend, llm_command = select_worker_llm_command(env)
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

    try:
        completed = run_command_process(
            llm_command,
            cwd=workspace,
            env=llm_env,
            timeout_seconds=llm_timeout_seconds,
            stdin_text=prompt_text if not command_uses_file_contract(llm_command) else None,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(_coerce_subprocess_output(exc.stdout), encoding="utf-8")
        stderr_path.write_text(_coerce_subprocess_output(exc.stderr), encoding="utf-8")
        report_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "objective": objective,
                    "strategy": strategy,
                    "worker_backend": "agent",
                    "llm_backend": llm_backend,
                    "validation_profile": "generic",
                    "worker_outcome": "blocked",
                    "blocked": True,
                    "infrastructure_failure": True,
                    "failure_category": "executor_timeout",
                    "failure_message": f"{llm_backend} worker command timed out",
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
        python_files = _run_bounded_process(
            ["git", "-C", str(workspace), "diff", "--name-only", "--", "*.py"],
            cwd=workspace,
            timeout_seconds=git_timeout_seconds,
        ).stdout.strip()
    except subprocess.TimeoutExpired as exc:
        git_timed_out = True
        python_files = ""
        git_failure_message = (
            f"Git metadata scan for changed Python files exceeded {git_timeout_seconds} seconds and was terminated."
        )
        compile_output_path.write_text(_coerce_subprocess_output(exc.stderr), encoding="utf-8")
    if python_files:
        try:
            compile_completed = _run_bounded_process(
                ["python3", "-m", "py_compile", *python_files.splitlines()],
                cwd=workspace,
                timeout_seconds=compile_timeout_seconds,
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
        if git_timed_out:
            compile_output_path.write_text(git_failure_message + "\n", encoding="utf-8")
        else:
            compile_output_path.write_text("No Python files changed.\n", encoding="utf-8")
        compile_rc = 0

    test_command = _focused_test_command(strategy)
    validation_selection = "lightweight_repair" if strategy in LIGHTWEIGHT_REPAIR_STRATEGIES else "default_focused"
    test_timed_out = False
    try:
        test_completed = subprocess.run(
            test_command,
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=test_timeout_seconds,
        )
        test_output_path.write_text((test_completed.stdout or "") + (test_completed.stderr or ""), encoding="utf-8")
    except subprocess.TimeoutExpired as exc:
        test_timed_out = True
        test_completed = subprocess.CompletedProcess(
            args=test_command,
            returncode=124,
            stdout=_coerce_subprocess_output(exc.stdout),
            stderr=_coerce_subprocess_output(exc.stderr),
        )
        timeout_summary = (
            f"Focused unit-test validation hit the {test_timeout_seconds}s ceiling and was terminated.\n"
        )
        test_output_path.write_text(
            timeout_summary + (test_completed.stdout or "") + (test_completed.stderr or ""),
            encoding="utf-8",
        )

    try:
        changed_stdout = _run_bounded_process(
            ["git", "-C", str(workspace), "diff", "--name-only"],
            cwd=workspace,
            timeout_seconds=git_timeout_seconds,
        ).stdout
    except subprocess.TimeoutExpired:
        git_timed_out = True
        changed_stdout = ""
        if not git_failure_message:
            git_failure_message = (
                f"Git metadata scan exceeded {git_timeout_seconds} seconds and was terminated."
            )
    changed_files = [line.strip() for line in changed_stdout.splitlines() if line.strip()]
    try:
        untracked_stdout = _run_bounded_process(
            ["git", "-C", str(workspace), "ls-files", "--others", "--exclude-standard"],
            cwd=workspace,
            timeout_seconds=git_timeout_seconds,
        ).stdout
    except subprocess.TimeoutExpired:
        git_timed_out = True
        untracked_stdout = ""
        if not git_failure_message:
            git_failure_message = (
                f"Git metadata scan exceeded {git_timeout_seconds} seconds and was terminated."
            )
    untracked_files = [line.strip() for line in untracked_stdout.splitlines() if line.strip()]
    all_changed = sorted(dict.fromkeys(changed_files + untracked_files))
    test_files = [
        path for path in all_changed if "/test" in path or path.startswith("tests/") or path.endswith("_test.py") or path.endswith(".test.js")
    ]
    summary_text = _first_nonempty_line(completed.stdout)
    if not summary_text and stdout_path.exists():
        summary_text = _first_nonempty_line(stdout_path.read_text(encoding="utf-8", errors="replace"))

    llm_failed = completed.returncode != 0
    worker_outcome = "blocked" if llm_failed else "success"
    failure_message = _first_nonempty_line(completed.stderr) or _first_nonempty_line(completed.stdout)
    if not llm_failed and (compile_rc != 0 or test_completed.returncode != 0 or git_timed_out):
        worker_outcome = "failed"
    payload = {
        "task_id": task_id,
        "run_id": run_id,
        "objective": objective,
        "strategy": strategy,
        "worker_backend": "agent",
        "llm_backend": llm_backend,
        "validation_profile": "generic",
        "worker_outcome": worker_outcome,
        "changed_files": all_changed,
        "test_files": test_files or ["tests/test_cli.py"],
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
            "selection": validation_selection,
            "timeout_seconds": test_timeout_seconds,
            "timed_out": test_timed_out,
        },
        "summary": summary_text,
        "command": llm_command,
    }
    if test_timed_out:
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
    elif git_timed_out:
        payload.update(
            {
                "failure_category": "git_timeout",
                "failure_message": git_failure_message,
            }
        )
    if llm_failed:
        payload.update(
            {
                "blocked": True,
                "infrastructure_failure": True,
                "failure_category": "executor_process_failure",
                "failure_message": failure_message or f"{llm_backend} worker exited non-zero",
                "llm_returncode": completed.returncode,
            }
        )
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    if llm_failed or compile_rc != 0 or test_completed.returncode != 0:
        return 1
    return 0
