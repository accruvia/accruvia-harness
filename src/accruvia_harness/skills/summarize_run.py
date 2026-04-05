"""The /summarize-run skill — deterministic human-readable run summary.

Reads a run's artifact JSON files and produces a structured summary with a
~300-word markdown blob covering what was scoped, changed, validated, and
reviewed.

Deterministic skill (no LLM).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import SkillResult


_ARTIFACT_NAMES = [
    "scope_output.json",
    "implementation_output.json",
    "apply_changes_summary.json",
    "self_review_output.json",
    "validation_output.json",
    "diagnosis_output.json",
]


class SummarizeRunSkill:
    """Read run artifacts and emit a human-readable summary."""

    name = "summarize_run"
    output_schema: dict[str, Any] = {
        "required": [
            "task_title",
            "scope_approach",
            "files_written",
            "files_rejected",
            "edits_applied",
            "new_files_created",
            "validation_overall",
            "ship_ready",
            "summary_markdown",
        ],
        "types": {
            "task_title": "str",
            "scope_approach": "str",
            "files_written": "list",
            "files_rejected": "list",
            "edits_applied": "int",
            "new_files_created": "int",
            "validation_overall": "str",
            "ship_ready": "bool",
            "summary_markdown": "str",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        return ""  # deterministic skill, no LLM

    def parse_response(self, response_text: str) -> dict[str, Any]:
        return {}  # deterministic skill, no LLM

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        errors: list[str] = []
        for field in self.output_schema["required"]:
            if field not in parsed:
                errors.append(f"missing required field: {field}")
        if not isinstance(parsed.get("task_title"), str):
            errors.append("task_title must be a str")
        if not isinstance(parsed.get("scope_approach"), str):
            errors.append("scope_approach must be a str")
        if not isinstance(parsed.get("files_written"), list):
            errors.append("files_written must be a list")
        if not isinstance(parsed.get("files_rejected"), list):
            errors.append("files_rejected must be a list")
        ea = parsed.get("edits_applied")
        if not isinstance(ea, int) or isinstance(ea, bool):
            errors.append("edits_applied must be an int")
        nfc = parsed.get("new_files_created")
        if not isinstance(nfc, int) or isinstance(nfc, bool):
            errors.append("new_files_created must be an int")
        if not isinstance(parsed.get("validation_overall"), str):
            errors.append("validation_overall must be a str")
        if not isinstance(parsed.get("ship_ready"), bool):
            errors.append("ship_ready must be a bool")
        if not isinstance(parsed.get("summary_markdown"), str):
            errors.append("summary_markdown must be a str")
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
        run_dir: Path,
        task_title: str,
    ) -> SkillResult:
        """Read run artifacts and produce a human-readable summary."""
        artifacts: dict[str, dict[str, Any]] = {}
        missing_artifacts: list[str] = []

        for name in _ARTIFACT_NAMES:
            path = run_dir / name
            if path.exists():
                try:
                    artifacts[name] = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    missing_artifacts.append(name)
            else:
                missing_artifacts.append(name)

        # Extract fields from artifacts with safe defaults
        scope = artifacts.get("scope_output.json", {})
        impl = artifacts.get("implementation_output.json", {})
        apply_summary = artifacts.get("apply_changes_summary.json", {})
        review = artifacts.get("self_review_output.json", {})
        validation = artifacts.get("validation_output.json", {})
        diagnosis = artifacts.get("diagnosis_output.json", {})

        scope_approach = str(scope.get("approach", ""))
        files_to_touch = scope.get("files_to_touch", [])
        risks = scope.get("risks", [])

        edits_applied = apply_summary.get("edits_applied", 0)
        new_files_created = apply_summary.get("new_files_created", 0)
        files_rejected = apply_summary.get("rejected", [])
        files_written = list(files_to_touch)

        validation_overall = str(validation.get("overall", "unknown"))
        ship_ready = bool(review.get("ship_ready", False))

        review_issues = review.get("issues", [])
        impl_rationale = str(impl.get("rationale", ""))
        diagnosis_root_cause = str(diagnosis.get("root_cause", ""))

        # Build ~300-word markdown summary
        lines: list[str] = []

        lines.append("## Task")
        lines.append("")
        lines.append(f"**{task_title}**")
        lines.append("")
        if scope_approach:
            lines.append(f"Approach: {scope_approach}")
            lines.append("")
        if risks:
            lines.append("Risks identified: " + "; ".join(str(r) for r in risks))
            lines.append("")

        lines.append("## What changed")
        lines.append("")
        if files_to_touch:
            lines.append("Files in scope: " + ", ".join(str(f) for f in files_to_touch))
        lines.append(f"Edits applied: {edits_applied}")
        lines.append(f"New files created: {new_files_created}")
        if files_rejected:
            rej_names = [
                str(r.get("path", r) if isinstance(r, dict) else r)
                for r in files_rejected
            ]
            lines.append(f"Rejected: {', '.join(rej_names)}")
        if impl_rationale:
            lines.append(f"Rationale: {impl_rationale}")
        lines.append("")

        lines.append("## Validation")
        lines.append("")
        lines.append(f"Overall: **{validation_overall}**")
        val_results = validation.get("results", [])
        for entry in val_results:
            entry_name = str(entry.get("name", ""))
            status = str(entry.get("status", ""))
            lines.append(f"- {entry_name}: {status}")
        lines.append("")

        lines.append("## Review")
        lines.append("")
        lines.append(f"Ship-ready: **{'yes' if ship_ready else 'no'}**")
        if review_issues:
            lines.append("Issues:")
            for issue in review_issues:
                lines.append(f"- {issue}")
        if diagnosis_root_cause:
            lines.append(f"Diagnosis: {diagnosis_root_cause}")
        if missing_artifacts:
            lines.append("")
            lines.append(f"Missing artifacts: {', '.join(missing_artifacts)}")

        summary_markdown = "\n".join(lines)

        output: dict[str, Any] = {
            "task_title": task_title,
            "scope_approach": scope_approach,
            "files_written": files_written,
            "files_rejected": files_rejected,
            "edits_applied": edits_applied,
            "new_files_created": new_files_created,
            "validation_overall": validation_overall,
            "ship_ready": ship_ready,
            "summary_markdown": summary_markdown,
        }
        if missing_artifacts:
            output["missing_artifacts"] = missing_artifacts

        return SkillResult(
            skill_name=self.name,
            success=True,
            output=output,
        )
