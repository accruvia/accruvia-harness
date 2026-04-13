"""UI responder skill — answers operator comments from current harness state."""
from __future__ import annotations

import json
from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


_RECOMMENDED_ACTIONS = (
    "none",
    "answer_prompt",
    "review_mermaid",
    "review_run",
    "start_run",
    "open_investigation",
)
_MODE_SHIFTS = ("none", "investigation")


class UIResponderSkill:
    name = "ui_responder"
    output_schema: dict[str, Any] = {
        "required": ["reply", "recommended_action", "evidence_refs", "mode_shift"],
        "types": {
            "reply": "str",
            "recommended_action": "str",
            "evidence_refs": "list",
            "mode_shift": "str",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        context_payload = inputs.get("context_payload") if isinstance(inputs.get("context_payload"), dict) else {}
        operator_message = str(inputs.get("operator_message") or "")
        return (
            "You are the accruvia-harness UI responder.\n"
            "Answer the operator's latest message directly and concretely.\n"
            "Use the full current objective context, not just the latest run.\n"
            "Do not dodge the question. Do not default to boilerplate about reviewing output unless that directly answers the question.\n"
            "Prefer plain language and explain what stage the operator is in when relevant.\n"
            "Return JSON only with keys: reply, recommended_action, evidence_refs, mode_shift.\n"
            f"reply: short plain-language answer to the operator\n"
            f"recommended_action: one of {', '.join(_RECOMMENDED_ACTIONS)}\n"
            "evidence_refs: array of short strings\n"
            f"mode_shift: one of {', '.join(_MODE_SHIFTS)}\n\n"
            f"Operator message: {operator_message}\n\n"
            f"Context:\n{json.dumps(context_payload, indent=2, sort_keys=True, default=str)}\n"
        )

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        reply = str(parsed.get("reply") or "").strip()
        recommended_action = str(parsed.get("recommended_action") or "none").strip() or "none"
        if recommended_action not in _RECOMMENDED_ACTIONS:
            recommended_action = "none"
        mode_shift = str(parsed.get("mode_shift") or "none").strip() or "none"
        if mode_shift not in _MODE_SHIFTS:
            mode_shift = "none"
        evidence_refs = [
            str(item).strip()
            for item in list(parsed.get("evidence_refs") or [])
            if str(item).strip()
        ]
        return {
            "reply": reply,
            "recommended_action": recommended_action,
            "mode_shift": mode_shift,
            "evidence_refs": evidence_refs,
        }

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        if not parsed.get("reply"):
            return False, ["reply is empty"]
        return True, []

    def materialize(self, store: Any, result: SkillResult, inputs: dict[str, Any]) -> None:
        return None
