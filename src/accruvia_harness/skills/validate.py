"""The /validate skill — deterministic compile + test runner.

Unlike other skills, /validate does NOT call an LLM. It runs a configurable
set of shell commands in the workspace (compile, test, lint) and returns a
structured result. This replaces the fragile report.json contract — the
data is produced directly, not scraped out of a worker-written file.

The caller provides a list of named commands. /validate runs them in order,
captures stdout/stderr/exit_code, and returns pass/fail per command plus an
overall verdict.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from .base import SkillResult


# Profile-specific defaults. Callers can override by passing their own commands.
_PROFILE_COMMANDS: dict[str, list[dict[str, Any]]] = {
    "python": [
        {"name": "compile", "cmd": "python -m compileall -q .", "timeout": 120},
        {"name": "tests", "cmd": "python -m pytest -q --no-header -x", "timeout": 600},
    ],
    "javascript": [
        {"name": "build", "cmd": "npm run build --if-present", "timeout": 300},
        {"name": "tests", "cmd": "npm test --silent", "timeout": 600},
    ],
    "generic": [
        {"name": "check", "cmd": "make test", "timeout": 600},
    ],
    "lightweight_operator": [],  # no validation, for small UX tweaks
}


def commands_for_profile(profile: str) -> list[dict[str, Any]]:
    """Return the default command list for a validation profile."""
    return list(_PROFILE_COMMANDS.get(profile, _PROFILE_COMMANDS["generic"]))


class ValidateSkill:
    """Deterministic validation runner. Conforms to the Skill interface but
    does not invoke an LLM.
    """

    name = "validate"
    output_schema: dict[str, Any] = {
        "required": ["overall", "results"],
        "types": {
            "overall": "str",
            "results": "list",
            "failure_evidence": "str",
            "elapsed_seconds": "float",
        },
        "allowed_values": {"overall": ["pass", "fail", "skipped"]},
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        return ""  # not used; /validate is deterministic

    def parse_response(self, response_text: str) -> dict[str, Any]:
        return {}  # not used; /validate is deterministic

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if parsed.get("overall") not in {"pass", "fail", "skipped"}:
            errors.append(f"invalid overall: {parsed.get('overall')}")
        if not isinstance(parsed.get("results"), list):
            errors.append("results must be a list")
        return (len(errors) == 0, errors)

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        return None

    def invoke_deterministic(
        self,
        workspace_root: Path,
        commands: list[dict[str, Any]],
        run_dir: Path,
    ) -> SkillResult:
        """Run commands in workspace_root and return a structured SkillResult.

        Each command entry: {name: str, cmd: str, timeout: int (optional), cwd: str (optional)}
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        if not commands:
            return SkillResult(
                skill_name=self.name,
                success=True,
                output={
                    "overall": "skipped",
                    "results": [],
                    "failure_evidence": "",
                    "elapsed_seconds": 0.0,
                },
            )

        results: list[dict[str, Any]] = []
        overall = "pass"
        failure_evidence = ""
        started = time.monotonic()
        for entry in commands:
            name = str(entry.get("name") or "command")
            cmd = str(entry.get("cmd") or "").strip()
            if not cmd:
                results.append({"name": name, "status": "skipped", "exit_code": 0})
                continue
            timeout = int(entry.get("timeout") or 300)
            cwd = Path(entry.get("cwd") or workspace_root)
            log_path = run_dir / f"validate_{name}.log"
            cmd_start = time.monotonic()
            try:
                completed = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=cwd,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
                exit_code = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except subprocess.TimeoutExpired as exc:
                exit_code = 124
                stdout = exc.stdout.decode("utf-8", errors="ignore") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                stderr = exc.stderr.decode("utf-8", errors="ignore") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            except (OSError, subprocess.SubprocessError) as exc:
                exit_code = 127
                stdout = ""
                stderr = f"failed to launch: {exc}"

            elapsed = time.monotonic() - cmd_start
            log_path.write_text(
                f"$ {cmd}\n# cwd={cwd}\n# exit_code={exit_code}\n# elapsed={elapsed:.1f}s\n"
                f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}",
                encoding="utf-8",
            )
            status = "pass" if exit_code == 0 else "fail"
            results.append(
                {
                    "name": name,
                    "status": status,
                    "exit_code": exit_code,
                    "elapsed_seconds": round(elapsed, 2),
                    "log_path": str(log_path),
                }
            )
            if status == "fail":
                overall = "fail"
                # First failure provides evidence; don't run remaining commands
                combined = (stdout + "\n" + stderr).strip()
                failure_evidence = combined[-4000:]
                break

        return SkillResult(
            skill_name=self.name,
            success=True,  # skill invocation succeeded; overall=pass/fail indicates validation result
            output={
                "overall": overall,
                "results": results,
                "failure_evidence": failure_evidence,
                "elapsed_seconds": round(time.monotonic() - started, 2),
            },
        )
