"""The /post-merge-check skill — SRE verification after promotion_apply.

Runs after a merge lands on the target branch. Verifies that main is still
healthy: tests still pass on the new HEAD. If not, signals rollback.

Closes the gap in CONTROL-PLANE-PLAN.md:82-83 — previously there was no
post-merge validation. If a merge silently broke main, the next run would
inherit the break.

Deterministic skill (no LLM). Runs the same validation command set as
/validate and reports a rollback recommendation if anything fails.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import SkillResult
from .validate import ValidateSkill, commands_for_profile


class PostMergeCheckSkill:
    """Re-runs validation on the post-merge HEAD and flags rollback if broken."""

    name = "post_merge_check"
    output_schema: dict[str, Any] = {
        "required": ["main_healthy", "rollback_needed"],
        "types": {
            "main_healthy": "bool",
            "rollback_needed": "bool",
            "validation": "dict",
            "failed_stage": "str",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        return ""

    def parse_response(self, response_text: str) -> dict[str, Any]:
        return {}

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not isinstance(parsed.get("main_healthy"), bool):
            errors.append("main_healthy must be a bool")
        if not isinstance(parsed.get("rollback_needed"), bool):
            errors.append("rollback_needed must be a bool")
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
        validation_profile: str,
        run_dir: Path,
    ) -> SkillResult:
        """Run the profile's validation commands on the post-merge workspace."""
        validate_skill = ValidateSkill()
        commands = commands_for_profile(validation_profile)
        validation = validate_skill.invoke_deterministic(
            workspace_root=workspace,
            commands=commands,
            run_dir=run_dir,
        )
        overall = str(validation.output.get("overall") or "skipped")
        healthy = overall in {"pass", "skipped"}
        failed_stage = ""
        if not healthy:
            for entry in validation.output.get("results") or []:
                if str(entry.get("status")) == "fail":
                    failed_stage = str(entry.get("name") or "")
                    break
        return SkillResult(
            skill_name=self.name,
            success=True,
            output={
                "main_healthy": healthy,
                "rollback_needed": not healthy,
                "validation": validation.output,
                "failed_stage": failed_stage,
            },
        )
