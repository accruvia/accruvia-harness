"""Mermaid update proposal skill."""
from __future__ import annotations

import json
from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


class MermaidUpdateProposalSkill:
    name = "mermaid_update_proposal"
    output_schema: dict[str, Any] = {
        "required": ["proposed_content", "rationale"],
        "types": {"proposed_content": "str", "rationale": "str"},
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        objective_title = str(inputs.get("objective_title") or "")
        objective_summary = str(inputs.get("objective_summary") or "")
        intent_summary = str(inputs.get("intent_summary") or "")
        success_definition = str(inputs.get("success_definition") or "")
        non_negotiables = list(inputs.get("non_negotiables") or [])
        current_mermaid = str(inputs.get("current_mermaid") or "")
        directive = str(inputs.get("directive") or "")
        anchor_label = str(inputs.get("anchor_label") or "")
        rewrite_requested = bool(inputs.get("rewrite_requested") or False)
        comments = list(inputs.get("recent_comments") or [])
        prior_round_findings = [
            str(x).strip() for x in list(inputs.get("prior_round_findings") or []) if str(x).strip()
        ]
        round_number = int(inputs.get("round_number") or 1)
        prior_block = ""
        if prior_round_findings:
            prior_block = (
                f"\nThis is Mermaid red-team round {round_number}. The previous proposal failed "
                "the following red-team findings. Rewrite the diagram so each objection is "
                "addressed — do not repeat the same mistakes.\n"
                f"Prior-round findings:\n{json.dumps(prior_round_findings, indent=2)}\n"
            )

        edit_mode_instruction = (
            f"This is an anchored local edit request around the Mermaid element labeled '{anchor_label}'. "
            "Preserve the rest of the diagram unless the operator explicitly asks for broader restructuring. "
            "Make the smallest viable patch that satisfies the comment."
            if anchor_label and not rewrite_requested
            else "You may revise the full diagram as needed to satisfy the operator's requested process change."
        )
        return (
            "You are updating a Mermaid flowchart for the accruvia-harness UI.\n"
            "Revise the workflow_control Mermaid to reflect the operator's requested process changes.\n"
            "Preserve valid parts of the current diagram. Avoid unnecessary rewrites.\n"
            f"{edit_mode_instruction}\n"
            "Return JSON only with keys: proposed_content, rationale.\n"
            "proposed_content: full Mermaid flowchart text\n"
            "rationale: one short sentence explaining what changed\n\n"
            f"Objective title: {objective_title}\n"
            f"Objective summary: {objective_summary}\n"
            f"Intent summary: {intent_summary}\n"
            f"Success definition: {success_definition}\n"
            f"Non-negotiables: {json.dumps(non_negotiables)}\n"
            f"Current Mermaid:\n{current_mermaid}\n\n"
            f"Operator directive: {directive}\n"
            f"Recent operator comments: {json.dumps(comments, indent=2)}\n"
            f"{prior_block}"
        )

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        # Tolerate both {summary, content} and {rationale, proposed_content}
        proposed_content = parsed.get("proposed_content") or parsed.get("content") or ""
        rationale = parsed.get("rationale") or parsed.get("summary") or ""
        return {
            "proposed_content": str(proposed_content).strip(),
            "rationale": str(rationale).strip(),
        }

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        if not parsed.get("proposed_content"):
            return False, ["proposed_content is empty"]
        return True, []

    def materialize(self, store: Any, result: SkillResult, inputs: dict[str, Any]) -> None:
        return None
