"""The /promotion-apply skill — deterministic git merge/push.

Not an LLM call. Executes the approved promotion as git operations. Per
CONTROL-PLANE-PLAN.md, this should be owned by the control plane, not the
service layer. Exposing it as a skill lets either the control plane or the
promotion service invoke it with identical semantics.
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


class PromotionApplySkill:
    """Deterministic git merge + optional push."""

    name = "promotion_apply"
    output_schema: dict[str, Any] = {
        "required": ["merged", "commit_sha", "conflicts"],
        "types": {
            "merged": "bool",
            "commit_sha": "str",
            "conflicts": "list",
            "pushed": "bool",
            "stderr": "str",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        return ""

    def parse_response(self, response_text: str) -> dict[str, Any]:
        return {}

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not isinstance(parsed.get("merged"), bool):
            errors.append("merged must be a bool")
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
        source_branch: str,
        target_branch: str,
        push: bool = False,
        no_ff: bool = True,
        merge_message: str | None = None,
    ) -> SkillResult:
        """Fast-forward OR --no-ff merge source_branch into target_branch.

        Returns structured SkillResult with {merged, commit_sha, conflicts, pushed, stderr}.
        """
        workspace = Path(workspace)
        if not workspace.exists() or not (workspace / ".git").exists():
            return SkillResult(
                skill_name=self.name,
                success=False,
                errors=["workspace is not a git repository"],
            )

        # 1. Verify both branches exist
        rc, _, err = _git(["rev-parse", "--verify", source_branch], workspace)
        if rc != 0:
            return SkillResult(
                skill_name=self.name,
                success=False,
                output={"merged": False, "commit_sha": "", "conflicts": [], "pushed": False, "stderr": err},
                errors=[f"source branch not found: {source_branch}"],
            )
        rc, _, err = _git(["rev-parse", "--verify", target_branch], workspace)
        if rc != 0:
            return SkillResult(
                skill_name=self.name,
                success=False,
                output={"merged": False, "commit_sha": "", "conflicts": [], "pushed": False, "stderr": err},
                errors=[f"target branch not found: {target_branch}"],
            )

        # 2. Check working tree is clean
        rc, status_out, _ = _git(["status", "--porcelain"], workspace)
        if rc == 0 and status_out.strip():
            return SkillResult(
                skill_name=self.name,
                success=False,
                output={"merged": False, "commit_sha": "", "conflicts": [], "pushed": False, "stderr": "working tree is dirty"},
                errors=["working tree is dirty; refusing to merge"],
            )

        # 3. Checkout target
        rc, _, err = _git(["checkout", target_branch], workspace)
        if rc != 0:
            return SkillResult(
                skill_name=self.name,
                success=False,
                output={"merged": False, "commit_sha": "", "conflicts": [], "pushed": False, "stderr": err},
                errors=[f"failed to checkout {target_branch}"],
            )

        # 4. Merge
        merge_args = ["merge", source_branch]
        if no_ff:
            merge_args.append("--no-ff")
        if merge_message:
            merge_args.extend(["-m", merge_message])
        rc, _, err = _git(merge_args, workspace, timeout=120)
        if rc != 0:
            # Attempt to detect conflicts
            _, diff_out, _ = _git(["diff", "--name-only", "--diff-filter=U"], workspace)
            conflicts = [line.strip() for line in diff_out.splitlines() if line.strip()]
            # Abort the merge to leave working tree clean
            _git(["merge", "--abort"], workspace)
            return SkillResult(
                skill_name=self.name,
                success=False,
                output={"merged": False, "commit_sha": "", "conflicts": conflicts, "pushed": False, "stderr": err},
                errors=[f"merge failed: {err.splitlines()[0] if err else 'unknown'}"],
            )

        # 5. Capture commit sha
        rc, sha_out, _ = _git(["rev-parse", "HEAD"], workspace)
        commit_sha = sha_out.strip() if rc == 0 else ""

        # 6. Optional push
        pushed = False
        push_err = ""
        if push:
            rc, _, push_err = _git(["push", "origin", target_branch], workspace, timeout=120)
            pushed = rc == 0

        return SkillResult(
            skill_name=self.name,
            success=True,
            output={
                "merged": True,
                "commit_sha": commit_sha,
                "conflicts": [],
                "pushed": pushed,
                "stderr": push_err,
            },
        )
