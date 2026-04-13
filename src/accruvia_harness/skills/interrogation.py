"""Interrogation skill — red-team an objective before Mermaid review."""
from __future__ import annotations

import json
from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


class InterrogationSkill:
    name = "interrogation"
    output_schema: dict[str, Any] = {
        "required": ["questions", "red_team_findings", "ready_for_mermaid_review"],
        "types": {
            "questions": "list",
            "red_team_findings": "list",
            "ready_for_mermaid_review": "bool",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        objective_title = str(inputs.get("objective_title") or "")
        objective_summary = str(inputs.get("objective_summary") or "")
        intent_summary = str(inputs.get("intent_summary") or "")
        success_definition = str(inputs.get("success_definition") or "")
        non_negotiables = list(inputs.get("non_negotiables") or [])
        comments = list(inputs.get("recent_comments") or [])
        deterministic = inputs.get("deterministic_review") if isinstance(inputs.get("deterministic_review"), dict) else {}
        prior_round_findings = [
            str(x).strip() for x in list(inputs.get("prior_round_findings") or []) if str(x).strip()
        ]
        round_number = int(inputs.get("round_number") or 1)
        prior_block = ""
        if prior_round_findings:
            prior_block = (
                f"\nThis is red-team round {round_number}. The previous candidate failed the "
                "following red-team findings. Address each one directly in this round — do not "
                "repeat the same mistakes.\n"
                f"Prior-round findings:\n{json.dumps(prior_round_findings, indent=2)}\n"
            )
        return (
            "You are red-teaming a software objective before process review.\n"
            "Interrogate the objective. Extract plan elements. List the sharpest unresolved questions.\n"
            "Return JSON only with keys: summary, plan_elements, questions, red_team_findings, ready_for_mermaid_review.\n"
            "summary: short paragraph\n"
            "plan_elements: array of concise strings\n"
            "questions: array of concise red-team questions\n"
            "red_team_findings: array of concise objections (may be empty)\n"
            "ready_for_mermaid_review: bool — true only if the questions are already answered and findings are empty\n\n"
            f"Objective title: {objective_title}\n"
            f"Objective summary: {objective_summary}\n"
            f"Intent summary: {intent_summary}\n"
            f"Success definition: {success_definition}\n"
            f"Non-negotiables: {json.dumps(non_negotiables)}\n"
            f"Recent operator comments: {json.dumps(comments, indent=2)}\n"
            f"Deterministic review: {json.dumps(deterministic, indent=2, sort_keys=True)}\n"
            f"{prior_block}"
        )

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        out: dict[str, Any] = {
            "summary": str(parsed.get("summary") or "").strip(),
            "plan_elements": [
                str(x).strip() for x in list(parsed.get("plan_elements") or []) if str(x).strip()
            ],
            "questions": [
                str(x).strip() for x in list(parsed.get("questions") or []) if str(x).strip()
            ],
            "red_team_findings": [
                str(x).strip() for x in list(parsed.get("red_team_findings") or []) if str(x).strip()
            ],
        }
        ready = parsed.get("ready_for_mermaid_review")
        if isinstance(ready, str):
            ready = ready.strip().lower() in ("true", "yes", "1")
        out["ready_for_mermaid_review"] = bool(ready) if ready is not None else False
        return out

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        return validate_against_schema(parsed, self.output_schema)

    def materialize(self, store: Any, result: SkillResult, inputs: dict[str, Any]) -> None:
        return None
