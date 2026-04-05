"""The /benchmark skill — deterministic test-suite runtime baseline.

Runs a validation profile’s command set, measures per-command and total
elapsed time, and reports the slowest and failed commands.  Unlike /validate,
/benchmark runs ALL commands without short-circuiting on failure so every
command’s timing is captured.

Deterministic skill (no LLM).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from .base import SkillResult
from .validate import commands_for_profile


class BenchmarkSkill:
    """Run all profile commands, measure timings, report slowest and failed."""

    name = "benchmark"
    output_schema: dict[str, Any] = {
        "required": ["profile", "total_runtime_seconds", "test_count", "slowest", "failed"],
        "types": {
            "profile": "str",
            "total_runtime_seconds": "float",
            "test_count": "int",
            "slowest": "list",
            "failed": "list",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        return ""  # deterministic skill, no LLM

    def parse_response(self, response_text: str) -> dict[str, Any]:
        return {}  # deterministic skill, no LLM

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not isinstance(parsed.get("profile"), str):
            errors.append("profile must be a str")
        total = parsed.get("total_runtime_seconds")
        if not isinstance(total, (int, float)):
            errors.append("total_runtime_seconds must be a number")
        count = parsed.get("test_count")
        if not isinstance(count, int) or isinstance(count, bool):
            errors.append("test_count must be an int")
        if not isinstance(parsed.get("slowest"), list):
            errors.append("slowest must be a list")
        if not isinstance(parsed.get("failed"), list):
            errors.append("failed must be a list")
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
        validation_profile: str,
        run_dir: Path,
    ) -> SkillResult:
        """Run every command in the profile, measure timings, report results.

        Runs ALL commands regardless of individual exit codes.
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        commands = commands_for_profile(validation_profile)

        if not commands:
            return SkillResult(
                skill_name=self.name,
                success=True,
                output={
                    "profile": validation_profile,
                    "total_runtime_seconds": 0.0,
                    "test_count": 0,
                    "slowest": [],
                    "failed": [],
                },
            )

        timings: list[dict[str, Any]] = []  # {name, seconds}
        failed: list[dict[str, Any]] = []   # {name, exit_code}
        total_start = time.monotonic()

        for entry in commands:
            cmd_name = str(entry.get("name") or "command")
            cmd = str(entry.get("cmd") or "").strip()
            if not cmd:
                continue
            timeout = int(entry.get("timeout") or 300)
            cwd = Path(entry.get("cwd") or workspace_root)

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
            except subprocess.TimeoutExpired:
                exit_code = 124
            except (OSError, subprocess.SubprocessError):
                exit_code = 127

            elapsed = round(time.monotonic() - cmd_start, 4)
            timings.append({"name": cmd_name, "seconds": elapsed})
            if exit_code != 0:
                failed.append({"name": cmd_name, "exit_code": exit_code})

        total_elapsed = round(time.monotonic() - total_start, 4)
        # Top-3 slowest, sorted descending by seconds
        slowest = sorted(timings, key=lambda t: t["seconds"], reverse=True)[:3]

        return SkillResult(
            skill_name=self.name,
            success=True,
            output={
                "profile": validation_profile,
                "total_runtime_seconds": total_elapsed,
                "test_count": len(timings),
                "slowest": slowest,
                "failed": failed,
            },
        )
