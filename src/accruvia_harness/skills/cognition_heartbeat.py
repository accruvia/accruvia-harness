"""Cognition heartbeat skill.

Wraps the existing cognition-adapter prompt+parse path as a Skill so that
the heartbeat LLM call flows through invoke_skill. The adapter is supplied
via the inputs dict — the skill itself is adapter-agnostic.
"""
from __future__ import annotations

from typing import Any

from .base import SkillResult, validate_against_schema


class CognitionHeartbeatSkill:
    name = "cognition_heartbeat"
    output_schema: dict[str, Any] = {
        "required": ["summary", "issue_creation_needed", "proposed_tasks"],
        "types": {
            "summary": "str",
            "issue_creation_needed": "bool",
            "proposed_tasks": "list",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        # The cognition adapter owns prompt construction. The skill expects
        # the caller to pre-build the prompt and pass it in as `prompt`.
        prompt = inputs.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("cognition_heartbeat skill requires a non-empty 'prompt' input")
        return prompt

    def parse_response(self, response_text: str) -> dict[str, Any]:
        # The adapter parser is provided via inputs at invocation time, but
        # invoke_skill calls this method without context. We therefore stash
        # the adapter via a closure: see CognitionService.heartbeat.
        # Fallback: try our own minimal extraction.
        from .base import extract_json_payload

        parsed = extract_json_payload(response_text) or {}
        # Normalise the fields we care about
        out: dict[str, Any] = {
            "summary": str(parsed.get("summary") or "").strip(),
            "priority_focus": str(parsed.get("priority_focus") or "").strip(),
            "issue_creation_needed": bool(parsed.get("issue_creation_needed") or False),
            "proposed_tasks": list(parsed.get("proposed_tasks") or []),
        }
        if "next_heartbeat_seconds" in parsed:
            out["next_heartbeat_seconds"] = parsed["next_heartbeat_seconds"]
        return out

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        return validate_against_schema(parsed, self.output_schema)

    def materialize(self, store: Any, result: SkillResult, inputs: dict[str, Any]) -> None:
        return None
