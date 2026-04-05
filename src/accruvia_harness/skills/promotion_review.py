"""The /promotion-review skill — reviewer perspective for final gate.

Runs AFTER /self-review and /validate have already passed. This is the last
LLM-backed gate before git-level promotion. It asks: does this diff, viewed
against the task objective, actually deliver what was asked? Are there
concerns that weren't caught by tests?

Replaces the monolithic affirmation prompt in promotion_service.py that used
free-form text + heuristic parsing. Now: schema-enforced structured output.
"""
from __future__ import annotations

from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


_CONCERN_SEVERITIES = ("blocker", "concern", "note")


class PromotionReviewSkill:
    """Final-gate LLM review. Structured approve/reject decision."""

    name = "promotion_review"
    output_schema: dict[str, Any] = {
        "required": ["approved", "rationale", "concerns"],
        "types": {
            "approved": "bool",
            "rationale": "str",
            "concerns": "list",
            "summary": "str",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        title = str(inputs.get("title") or "").strip()
        objective = str(inputs.get("objective") or "").strip()
        diff = str(inputs.get("diff") or "").strip()
        validation_summary = str(inputs.get("validation_summary") or "").strip()
        scope_approach = str(inputs.get("scope_approach") or "").strip()
        changed_files = list(inputs.get("changed_files") or [])
        prior_concerns = list(inputs.get("prior_concerns") or [])

        prior_block = ""
        if prior_concerns:
            prior_block = (
                "Concerns from prior review rounds (the implementer claims to have "
                "addressed these; verify):\n"
                + "\n".join(f"  - {c}" for c in prior_concerns)
            )

        return "\n\n".join(
            filter(
                None,
                [
                    "You are the reviewer for a promotion gate. Tests have already "
                    "passed — do NOT re-check compile/test. Your job is to judge "
                    "whether the diff actually delivers the objective, and whether "
                    "there are risks or concerns that the tests did not catch.",
                    f"Task title: {title}",
                    f"Task objective: {objective}",
                    f"Scope approach: {scope_approach or '(unspecified)'}",
                    f"Changed files: {', '.join(changed_files) if changed_files else '(none listed)'}",
                    f"Validation summary: {validation_summary or '(passed)'}",
                    prior_block,
                    "Diff under review:\n" + (diff or "(empty diff)"),
                    "Return strict JSON with keys:\n"
                    "  approved (bool; true only if you would merge this as-is)\n"
                    "  rationale (string, 1-3 sentences justifying your decision)\n"
                    "  concerns (list of objects with keys 'severity' "
                    f"[one of {', '.join(_CONCERN_SEVERITIES)}] and 'description'; "
                    "empty list if none)\n"
                    "  summary (string, one sentence verdict)",
                    "Guidance: approve when the diff plainly satisfies the objective "
                    "and has no blocker concerns. Reject when the objective is not met, "
                    "when the diff introduces obvious risk, or when scope drift is "
                    "visible. Concerns at severity=note do NOT block approval.",
                ],
            )
        ).strip()

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        parsed.setdefault("summary", "")
        sr = parsed.get("approved")
        if isinstance(sr, str):
            parsed["approved"] = sr.strip().lower() in ("true", "yes", "approve", "approved", "1")
        if isinstance(parsed.get("concerns"), list):
            normalized: list[dict[str, str]] = []
            for item in parsed["concerns"]:
                if not isinstance(item, dict):
                    continue
                severity = str(item.get("severity") or "note").lower()
                if severity not in _CONCERN_SEVERITIES:
                    severity = "note"
                normalized.append(
                    {
                        "severity": severity,
                        "description": str(item.get("description") or "").strip(),
                    }
                )
            parsed["concerns"] = normalized
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        # Self-consistency: approved must be False if any concern is a blocker
        blockers = sum(
            1 for c in parsed.get("concerns") or []
            if isinstance(c, dict) and c.get("severity") == "blocker"
        )
        if blockers > 0 and parsed.get("approved"):
            parsed["approved"] = False
        if not parsed.get("rationale", "").strip():
            return False, ["rationale must be non-empty"]
        return True, []

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        return None

    @staticmethod
    def blocker_concerns(result: SkillResult) -> list[str]:
        return [
            str(c.get("description") or "")
            for c in result.output.get("concerns") or []
            if isinstance(c, dict) and c.get("severity") == "blocker"
        ]
