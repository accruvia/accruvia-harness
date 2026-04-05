"""The /self-review skill — staff-engineer critique of a diff.

Takes the unified diff produced by /implement and the original task context,
and returns structured issues + a ship_ready verdict. Replaces the "hope
the worker is honest with itself" pattern.

Distinct from /promotion-review: this runs WITHIN a work cycle before
promotion is even considered. It gives the orchestrator an early signal
about whether the implementation needs another /implement pass.
"""
from __future__ import annotations

from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


_SEVERITIES = ("blocker", "major", "minor", "nitpick")


class SelfReviewSkill:
    """Reviews a diff for defects, risks, and style issues."""

    name = "self_review"
    output_schema: dict[str, Any] = {
        "required": ["issues", "ship_ready", "summary"],
        "types": {
            "issues": "list",
            "ship_ready": "bool",
            "summary": "str",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        title = str(inputs.get("title") or "").strip()
        objective = str(inputs.get("objective") or "").strip()
        approach = str(inputs.get("approach") or "").strip()
        risks = list(inputs.get("risks") or [])
        diff = str(inputs.get("diff") or "").strip()

        risks_block = ""
        if risks:
            risks_block = "Scope flagged these risks — check the diff against them:\n" + "\n".join(
                f"  - {r}" for r in risks
            )

        return "\n\n".join(
            filter(
                None,
                [
                    "You are a staff engineer reviewing a diff for a single scoped "
                    "task. Your job is to find defects and clear blockers before the "
                    "code reaches validation. You are NOT reviewing for promotion — "
                    "tests will run next. Focus on: does the diff actually satisfy the "
                    "objective? Are there obvious bugs, type errors, missing imports, "
                    "or mis-scoped changes?",
                    f"Task title: {title}",
                    f"Task objective: {objective}",
                    f"Approach: {approach}",
                    risks_block,
                    "Diff under review:\n" + (diff or "(empty diff)"),
                    "Return strict JSON with keys:\n"
                    "  issues (list of objects, each with 'severity', 'file', 'description'; "
                    f"severity must be one of: {', '.join(_SEVERITIES)}; file may be empty "
                    "string if the issue is diff-wide)\n"
                    "  ship_ready (bool; true only if there are NO blocker or major issues)\n"
                    "  summary (string, 1-2 sentences)",
                    "Guidance: mark as blocker anything that WILL break tests or runtime. "
                    "Mark as major anything that likely breaks a non-obvious code path. "
                    "Minor and nitpick should NOT flip ship_ready to false.",
                ],
            )
        ).strip()

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        if isinstance(parsed.get("issues"), list):
            normalized: list[dict[str, str]] = []
            for item in parsed["issues"]:
                if not isinstance(item, dict):
                    continue
                severity = str(item.get("severity") or "minor").lower()
                if severity not in _SEVERITIES:
                    severity = "minor"
                normalized.append(
                    {
                        "severity": severity,
                        "file": str(item.get("file") or ""),
                        "description": str(item.get("description") or "").strip(),
                    }
                )
            parsed["issues"] = normalized
        # ship_ready from strings
        sr = parsed.get("ship_ready")
        if isinstance(sr, str):
            parsed["ship_ready"] = sr.strip().lower() in ("true", "yes", "1")
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        # Self-consistency check: ship_ready must be False when blocker/major issues present
        blockers = sum(
            1 for i in parsed.get("issues") or []
            if isinstance(i, dict) and i.get("severity") in {"blocker", "major"}
        )
        if blockers > 0 and parsed.get("ship_ready"):
            # auto-fix: downgrade ship_ready rather than reject
            parsed["ship_ready"] = False
        return True, []

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        return None

    @staticmethod
    def feedback_for_retry(result: SkillResult) -> str:
        """Extract a compact retry message from review issues."""
        issues = result.output.get("issues") or []
        blockers = [i for i in issues if i.get("severity") == "blocker"]
        majors = [i for i in issues if i.get("severity") == "major"]
        lines: list[str] = []
        for group, label in ((blockers, "BLOCKER"), (majors, "MAJOR")):
            for item in group:
                file_hint = f" ({item['file']})" if item.get("file") else ""
                lines.append(f"{label}{file_hint}: {item.get('description', '')}")
        summary = str(result.output.get("summary") or "").strip()
        if summary:
            lines.append(f"Summary: {summary}")
        return "\n".join(lines)
