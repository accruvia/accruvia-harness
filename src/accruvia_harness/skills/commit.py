"""The /commit skill — deterministic git staging and commit.

Not an LLM call. Stages specific files and commits with a message. Exposing it
as a skill lets the control plane or other services invoke it with identical
semantics.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .base import SkillResult


def _git(args: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        return 127, "", f"git invocation failed: {exc}"


class CommitSkill:
    """Deterministic git add + commit."""

    name = "commit"
    output_schema: dict[str, Any] = {
        "required": ["committed", "commit_sha", "staged"],
        "types": {
            "committed": "bool",
            "commit_sha": "str",
            "staged": "list",
            "stderr": "str",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        return ""

    def parse_response(self, response_text: str) -> dict[str, Any]:
        return {}

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not isinstance(parsed.get("committed"), bool):
            errors.append("committed must be a bool")
        if not isinstance(parsed.get("commit_sha"), str):
            errors.append("commit_sha must be a str")
        if not isinstance(parsed.get("staged"), list):
            errors.append("staged must be a list")
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
        *,
        workspace: Path,
        paths: list[str],
        message: str,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> SkillResult:
        """Stage specific paths and commit with message.

        Returns structured SkillResult with {committed, commit_sha, staged, stderr}.
        """
        workspace = Path(workspace)
        if not workspace.exists() or not (workspace / ".git").exists():
            return SkillResult(
                skill_name=self.name,
                success=False,
                errors=["workspace is not a git repository"],
            )

        # Empty paths = no-op
        if not paths:
            return SkillResult(
                skill_name=self.name,
                success=True,
                output={
                    "committed": False,
                    "commit_sha": "",
                    "staged": [],
                    "stderr": "",
                },
            )

        # 1. Stage specific paths
        rc, _, err = _git(["add", "--"] + list(paths), workspace)
        if rc != 0:
            return SkillResult(
                skill_name=self.name,
                success=False,
                output={"committed": False, "commit_sha": "", "staged": [], "stderr": err},
                errors=[f"git add failed: {err}"],
            )

        # 2. Commit (with optional author override)
        commit_args: list[str] = []
        if author_name and author_email:
            commit_args = [
                "-c", f"user.name={author_name}",
                "-c", f"user.email={author_email}",
            ]
        commit_cmd = commit_args + ["commit", "-m", message]
        rc, out, err = _git(commit_cmd, workspace)
        if rc != 0:
            return SkillResult(
                skill_name=self.name,
                success=False,
                output={"committed": False, "commit_sha": "", "staged": list(paths), "stderr": err},
                errors=[f"git commit failed: {err}"],
            )

        # 3. Capture commit sha
        rc, sha_out, _ = _git(["rev-parse", "HEAD"], workspace)
        commit_sha = sha_out.strip() if rc == 0 else ""

        return SkillResult(
            skill_name=self.name,
            success=True,
            output={
                "committed": True,
                "commit_sha": commit_sha,
                "staged": list(paths),
                "stderr": err,
            },
        )
