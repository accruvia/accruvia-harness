from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Mapping

from .llm import _coerce_subprocess_output, command_uses_file_contract, run_command_process


DEFAULT_AGENT_TEST_TIMEOUT_SECONDS = 300


def _agent_test_timeout_seconds(environ: Mapping[str, str]) -> int:
    raw_value = str(environ.get("ACCRUVIA_AGENT_TEST_TIMEOUT_SECONDS", "")).strip()
    if not raw_value:
        return DEFAULT_AGENT_TEST_TIMEOUT_SECONDS
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_AGENT_TEST_TIMEOUT_SECONDS
    return parsed if parsed > 0 else DEFAULT_AGENT_TEST_TIMEOUT_SECONDS


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
            timeout_seconds=None,
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

    python_files = subprocess.run(
        ["git", "-C", str(workspace), "diff", "--name-only", "--", "*.py"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if python_files:
        compile_completed = subprocess.run(
            ["python3", "-m", "py_compile", *python_files.splitlines()],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        compile_output_path.write_text(
            (compile_completed.stdout or "") + (compile_completed.stderr or ""),
            encoding="utf-8",
        )
        compile_rc = compile_completed.returncode
    else:
        compile_output_path.write_text("No Python files changed.\n", encoding="utf-8")
        compile_rc = 0

    test_command = [
        "python3",
        "-m",
        "unittest",
        "tests.test_cli",
        "tests.test_phase1",
        "tests.test_supervisor",
        "tests.test_observer",
    ]
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

    changed_files = [
        line.strip()
        for line in subprocess.run(
            ["git", "-C", str(workspace), "diff", "--name-only"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if line.strip()
    ]
    untracked_files = [
        line.strip()
        for line in subprocess.run(
            ["git", "-C", str(workspace), "ls-files", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if line.strip()
    ]
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
    if not llm_failed and (compile_rc != 0 or test_completed.returncode != 0):
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
        },
        "test_check": {
            "passed": test_completed.returncode == 0,
            "framework": "unittest",
            "output_path": str(test_output_path),
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
