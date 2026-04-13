"""sa-watch triage skill."""
from __future__ import annotations

from typing import Any

from .base import SkillResult, validate_against_schema


class SAWatchTriageSkill:
    name = "sa_watch_triage"
    output_schema: dict[str, Any] = {
        "required": ["report"],
        "types": {"report": "str"},
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        prompt = inputs.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("sa_watch_triage skill requires a non-empty 'prompt' input")
        return prompt

    def parse_response(self, response_text: str) -> dict[str, Any]:
        return {"report": response_text.strip()}

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        if not parsed.get("report"):
            return False, ["report is empty"]
        return True, []

    def materialize(self, store: Any, result: SkillResult, inputs: dict[str, Any]) -> None:
        return None
