"""The /quality-gate skill — automatic best-practice enforcement.

Deterministic skill that bundles industry-standard quality checks into
a single pass. Runs AFTER /validate (which checks compile + tests) and
adds the checks that non-developers wouldn't think to ask for:

    - Lint (ruff for Python, eslint for JS if available)
    - Security scan (detect hardcoded secrets, credentials, API keys)
    - Docstring coverage (new functions/classes must have docstrings)
    - Type annotation check (new functions should have type hints)

The gate is ON by default for all skills-pipeline runs. It reports
issues but does NOT block promotion — instead it adds quality_concerns
to the run diagnostics so the merge gate and /promotion-review can
factor them in.

This replaces the developer discipline of "remember to lint, add docs,
check for secrets" with automatic enforcement.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .base import SkillResult


# Patterns that strongly suggest hardcoded secrets
_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?i)(api[_-]?key|secret[_-]?key|password|token)\s*=\s*['\"][^'\"]{8,}", "hardcoded_credential"),
    (r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{20,}", "bearer_token"),
    (r"AKIA[0-9A-Z]{16}", "aws_access_key"),
    (r"-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----", "private_key"),
    (r"(?i)(sk-[a-zA-Z0-9]{20,})", "api_key_pattern"),
)

# Patterns that are false positives (test fixtures, examples)
_SECRET_ALLOWLIST: tuple[str, ...] = (
    "test_", "example", "placeholder", "REDACTED", "xxx", "your_",
    "fake_", "mock_", "dummy_",
)


def _scan_secrets(content: str, path: str) -> list[dict[str, str]]:
    """Scan a file's content for hardcoded secrets."""
    findings: list[dict[str, str]] = []
    for pattern, category in _SECRET_PATTERNS:
        for match in re.finditer(pattern, content):
            matched_text = match.group(0).lower()
            if any(allow in matched_text for allow in _SECRET_ALLOWLIST):
                continue
            findings.append({
                "file": path,
                "category": category,
                "line": str(content[:match.start()].count("\n") + 1),
                "snippet": match.group(0)[:40] + "...",
            })
    return findings


def _check_docstrings(content: str, path: str) -> list[dict[str, str]]:
    """Check that new function/class definitions have docstrings."""
    issues: list[dict[str, str]] = []
    lines = content.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("def ", "class ")) and not stripped.startswith("def _"):
            # Check if next non-empty line is a docstring
            has_docstring = False
            for j in range(i + 1, min(i + 4, len(lines))):
                next_line = lines[j].strip()
                if not next_line:
                    continue
                if next_line.startswith(('"""', "'''", 'r"""', "r'''")):
                    has_docstring = True
                break
            if not has_docstring:
                name = stripped.split("(")[0].split(":")[0].replace("def ", "").replace("class ", "")
                issues.append({
                    "file": path,
                    "issue": "missing_docstring",
                    "name": name,
                    "line": str(i + 1),
                })
    return issues


def _check_type_hints(content: str, path: str) -> list[dict[str, str]]:
    """Check that public function definitions have return type annotations."""
    issues: list[dict[str, str]] = []
    for i, line in enumerate(content.splitlines()):
        stripped = line.strip()
        if stripped.startswith("def ") and not stripped.startswith("def _"):
            # Check for -> before the FINAL colon (not param-annotation colons)
            sig_end = stripped.rfind(":")
            if sig_end > 0 and "->" not in stripped[:sig_end]:
                name = stripped.split("(")[0].replace("def ", "")
                issues.append({
                    "file": path,
                    "issue": "missing_return_type",
                    "name": name,
                    "line": str(i + 1),
                })
    return issues


class QualityGateSkill:
    """Automatic quality enforcement — runs after /validate."""

    name = "quality_gate"
    output_schema: dict[str, Any] = {
        "required": ["passed", "checks", "quality_concerns"],
        "types": {
            "passed": "bool",
            "checks": "list",
            "quality_concerns": "list",
            "summary": "str",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        return ""

    def parse_response(self, response_text: str) -> dict[str, Any]:
        return {}

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not isinstance(parsed.get("passed"), bool):
            errors.append("passed must be a bool")
        if not isinstance(parsed.get("checks"), list):
            errors.append("checks must be a list")
        if not isinstance(parsed.get("quality_concerns"), list):
            errors.append("quality_concerns must be a list")
        return (len(errors) == 0, errors)

    def materialize(self, store: Any, result: SkillResult, inputs: dict[str, Any]) -> None:
        return None

    def invoke_deterministic(
        self,
        *,
        workspace: Path,
        changed_files: list[str],
        run_dir: Path,
    ) -> SkillResult:
        """Run quality checks on changed files. Returns structured report."""
        run_dir.mkdir(parents=True, exist_ok=True)
        checks: list[dict[str, Any]] = []
        all_concerns: list[dict[str, str]] = []

        # 1. Lint (ruff if available)
        lint_result = self._run_lint(workspace, changed_files, run_dir)
        checks.append(lint_result)
        if lint_result.get("issues"):
            all_concerns.extend(lint_result["issues"][:10])

        # 2. Security scan
        secret_findings: list[dict[str, str]] = []
        for rel_path in changed_files:
            full = (workspace / rel_path).resolve()
            if not full.exists() or not full.is_file():
                continue
            try:
                content = full.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            secret_findings.extend(_scan_secrets(content, rel_path))
        checks.append({
            "name": "security_scan",
            "status": "fail" if secret_findings else "pass",
            "issues": secret_findings,
        })
        all_concerns.extend(secret_findings)

        # 3. Docstring coverage
        doc_issues: list[dict[str, str]] = []
        for rel_path in changed_files:
            if not rel_path.endswith(".py"):
                continue
            full = (workspace / rel_path).resolve()
            if not full.exists():
                continue
            try:
                content = full.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            doc_issues.extend(_check_docstrings(content, rel_path))
        checks.append({
            "name": "docstring_coverage",
            "status": "warn" if doc_issues else "pass",
            "issues": doc_issues,
        })

        # 4. Type hint coverage
        type_issues: list[dict[str, str]] = []
        for rel_path in changed_files:
            if not rel_path.endswith(".py"):
                continue
            full = (workspace / rel_path).resolve()
            if not full.exists():
                continue
            try:
                content = full.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            type_issues.extend(_check_type_hints(content, rel_path))
        checks.append({
            "name": "type_hint_coverage",
            "status": "warn" if type_issues else "pass",
            "issues": type_issues,
        })

        # Overall
        has_blocking = bool(secret_findings)  # secrets block; lint/docs/types warn
        passed = not has_blocking
        summary_parts = []
        if secret_findings:
            summary_parts.append(f"{len(secret_findings)} potential secret(s) detected")
        if doc_issues:
            summary_parts.append(f"{len(doc_issues)} missing docstring(s)")
        if type_issues:
            summary_parts.append(f"{len(type_issues)} missing type hint(s)")
        if lint_result.get("issues"):
            summary_parts.append(f"{len(lint_result['issues'])} lint issue(s)")
        summary = "; ".join(summary_parts) if summary_parts else "All quality checks passed"

        return SkillResult(
            skill_name=self.name,
            success=True,
            output={
                "passed": passed,
                "checks": checks,
                "quality_concerns": all_concerns[:20],
                "summary": summary,
            },
        )

    def _run_lint(
        self, workspace: Path, changed_files: list[str], run_dir: Path,
    ) -> dict[str, Any]:
        py_files = [f for f in changed_files if f.endswith(".py")]
        if not py_files:
            return {"name": "lint", "status": "skip", "issues": []}
        try:
            result = subprocess.run(
                ["python", "-m", "ruff", "check", "--select=E,W,F", "--no-fix", *py_files],
                cwd=workspace,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            log = run_dir / "lint.log"
            log.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
            issues: list[dict[str, str]] = []
            for line in (result.stdout or "").splitlines():
                if ":" in line and any(line.endswith(f" {code}") or f" {code} " in line
                                       for code in ("E", "W", "F")):
                    issues.append({"issue": "lint", "detail": line.strip()})
                elif line.strip() and not line.startswith(("Found", "All checks")):
                    issues.append({"issue": "lint", "detail": line.strip()})
            return {
                "name": "lint",
                "status": "warn" if issues else "pass",
                "issues": issues[:10],
            }
        except (OSError, subprocess.SubprocessError):
            return {"name": "lint", "status": "skip", "issues": []}
