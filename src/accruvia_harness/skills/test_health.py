"""The /test-health skill — deterministic test-suite hygiene check.

Collects tests via pytest --collect-only, runs BenchmarkSkill for timings,
scans for parallelism hazards, and returns health recommendations.

Deterministic skill (no LLM).
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .base import SkillResult
from .benchmark import BenchmarkSkill


class TestHealthSkill:
    """Assess test-suite hygiene: count, timings, duplicates, parallelism safety."""

    name = "test_health"
    output_schema: dict[str, Any] = {
        "required": [
            "profile", "total_tests", "total_runtime_seconds",
            "slowest", "duplicates", "parallelism_safe", "recommendations",
        ],
        "types": {
            "profile": "str",
            "total_tests": "int",
            "total_runtime_seconds": "float",
            "slowest": "list",
            "duplicates": "list",
            "parallelism_safe": "bool",
            "recommendations": "list",
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
        total_tests = parsed.get("total_tests")
        if not isinstance(total_tests, int) or isinstance(total_tests, bool):
            errors.append("total_tests must be an int")
        total_rt = parsed.get("total_runtime_seconds")
        if not isinstance(total_rt, (int, float)):
            errors.append("total_runtime_seconds must be a number")
        if not isinstance(parsed.get("slowest"), list):
            errors.append("slowest must be a list")
        if not isinstance(parsed.get("duplicates"), list):
            errors.append("duplicates must be a list")
        if not isinstance(parsed.get("parallelism_safe"), bool):
            errors.append("parallelism_safe must be a bool")
        if not isinstance(parsed.get("recommendations"), list):
            errors.append("recommendations must be a list")
        return (len(errors) == 0, errors)

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_tests(workspace_root: Path) -> list[str]:
        """Run pytest --collect-only -q and return the list of test node IDs."""
        try:
            proc = subprocess.run(
                ["python", "-m", "pytest", "--collect-only", "-q"],
                cwd=workspace_root,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            lines = proc.stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            return []

        collected: list[str] = []
        for line in lines:
            line = line.strip()
            # pytest -q outputs lines like "tests/test_foo.py::test_bar"
            # Skip summary lines (e.g. "5 tests collected") and blanks
            if not line or line.startswith("=") or line.startswith("-"):
                continue
            if "::" in line:
                collected.append(line)
            elif re.match(r"^\d+ tests?", line):
                # Summary line like "5 tests collected" â€” stop
                break
        return collected

    @staticmethod
    def _find_duplicates(test_ids: list[str]) -> list[str]:
        """Return test names (last component) that appear more than once."""
        names = [tid.split("::")[-1] for tid in test_ids]
        counts = Counter(names)
        return sorted(name for name, count in counts.items() if count > 1)

    @staticmethod
    def _check_parallelism_safe(workspace_root: Path) -> bool:
        """Return False if any test file imports tempfile AND uses chdir."""
        for test_file in workspace_root.rglob("test_*.py"):
            try:
                content = test_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            has_tempfile = "import tempfile" in content
            has_chdir = "chdir" in content
            if has_tempfile and has_chdir:
                return False
        # Also check *_test.py pattern
        for test_file in workspace_root.rglob("*_test.py"):
            try:
                content = test_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            has_tempfile = "import tempfile" in content
            has_chdir = "chdir" in content
            if has_tempfile and has_chdir:
                return False
        return True

    @staticmethod
    def _build_recommendations(
        total_tests: int,
        slowest: list[dict[str, Any]],
        parallelism_safe: bool,
    ) -> list[str]:
        """Generate actionable recommendation strings."""
        recs: list[str] = []
        if total_tests >= 20 and parallelism_safe:
            recs.append(
                f"Consider pytest-xdist for {total_tests} tests â€” "
                f"potential wall-clock reduction with parallel execution"
            )
        if slowest:
            # Flag tests over 5 seconds as slow-test candidates
            slow_names = [s["name"] for s in slowest if s.get("seconds", 0) > 5]
            if slow_names:
                recs.append(
                    f"Mark tests in {', '.join(slow_names)} for @pytest.mark.slow"
                )
        if not parallelism_safe:
            recs.append(
                "Some test files use tempfile + chdir â€” "
                "refactor before enabling parallel execution"
            )
        return recs

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def invoke_deterministic(
        self,
        workspace_root: Path,
        validation_profile: str,
        run_dir: Path,
    ) -> SkillResult:
        """Analyse test-suite health for the given workspace and profile."""
        run_dir.mkdir(parents=True, exist_ok=True)

        # 1. Collect tests via pytest
        test_ids = self._collect_tests(workspace_root)
        total_tests = len(test_ids)

        # 2. Detect duplicate test names
        duplicates = self._find_duplicates(test_ids)

        # 3. Run BenchmarkSkill for timings
        bench = BenchmarkSkill()
        bench_result = bench.invoke_deterministic(
            workspace_root=workspace_root,
            validation_profile=validation_profile,
            run_dir=run_dir / "_benchmark",
        )
        total_runtime = bench_result.output.get("total_runtime_seconds", 0.0)

        # Extract all timings from benchmark and take top-5 slowest
        # BenchmarkSkill only exposes top-3 in its output, so we get what's
        # available and report up to 5.
        bench_slowest = bench_result.output.get("slowest", [])
        slowest = bench_slowest[:5]

        # 4. Check parallelism safety
        parallelism_safe = self._check_parallelism_safe(workspace_root)

        # 5. Build recommendations
        recommendations = self._build_recommendations(
            total_tests, slowest, parallelism_safe,
        )

        return SkillResult(
            skill_name=self.name,
            success=True,
            output={
                "profile": validation_profile,
                "total_tests": total_tests,
                "total_runtime_seconds": total_runtime,
                "slowest": slowest,
                "duplicates": duplicates,
                "parallelism_safe": parallelism_safe,
                "recommendations": recommendations,
            },
        )
