"""Atomic decomposition skill.

Single LLM call that produces a list of atomic implementation units from
the objective + intent + accepted Mermaid + repo context. Replaces the
previous multi-round generate/critique/refine loop in ui.py.
"""
from __future__ import annotations

import json
from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


class AtomicDecompositionSkill:
    name = "atomic_decomposition"
    output_schema: dict[str, Any] = {
        "required": ["units"],
        "types": {"units": "list"},
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        objective_title = str(inputs.get("objective_title") or "")
        objective_summary = str(inputs.get("objective_summary") or "")
        intent_summary = str(inputs.get("intent_summary") or "")
        success_definition = str(inputs.get("success_definition") or "")
        non_negotiables = list(inputs.get("non_negotiables") or [])
        mermaid_content = str(inputs.get("mermaid_content") or "")
        repo_context = str(inputs.get("repo_context") or "")
        comments = list(inputs.get("recent_comments") or [])
        prior_round_findings = [
            str(x).strip() for x in list(inputs.get("prior_round_findings") or []) if str(x).strip()
        ]
        round_number = int(inputs.get("round_number") or 1)
        prior_block = ""
        if prior_round_findings:
            prior_block = (
                f"\nThis is decomposition round {round_number}. The previous candidate failed "
                "the following red-team findings. Rework the units so each objection is "
                "addressed — do not repeat the same mistakes.\n"
                f"Prior-round findings:\n{json.dumps(prior_round_findings, indent=2)}\n"
            )
        return (
            "You are decomposing a software objective into ATOMIC implementation units.\n\n"
            "DEFINITION OF ATOMIC:\n"
            "An atomic unit is the smallest possible unit of work — ideally a single function,\n"
            "at most one file or one tightly-coupled page of code. Think: one function, one\n"
            "test, one reviewable diff. Do NOT bundle unrelated changes.\n\n"
            "Return JSON only: {\"units\": [...]}\n"
            "Each unit has keys: title, objective, rationale, strategy, files_involved.\n"
            "- title: short imperative phrase naming the exact function or class\n"
            "- objective: 2-4 sentences naming exact file, class, function and acceptance test\n"
            "- rationale: why this is a separate unit\n"
            "- strategy: 'atomic_from_mermaid'\n"
            "- files_involved: list of 1-2 file paths this unit will touch\n"
            "Generate as many units as the objective requires. Do NOT cap the count.\n"
            "Order by dependency: earlier units must not depend on later ones.\n"
            "Each unit must map to a node or edge in the accepted Mermaid flowchart.\n"
            "Units must not overlap.\n\n"
            f"Objective title: {objective_title}\n"
            f"Objective summary: {objective_summary}\n"
            f"Intent summary: {intent_summary}\n"
            f"Success definition: {success_definition}\n"
            f"Non-negotiables: {json.dumps(non_negotiables)}\n"
            f"Accepted Mermaid:\n{mermaid_content}\n\n"
            f"Recent operator comments:\n{json.dumps(comments, indent=2)}\n\n"
            f"Repo context:\n{repo_context}\n"
            f"{prior_block}"
        )

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {"units": []}
        units = parsed.get("units")
        if not isinstance(units, list):
            return {"units": []}
        normalized: list[dict[str, Any]] = []
        for item in units:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "title": str(item.get("title") or "").strip(),
                    "objective": str(item.get("objective") or "").strip(),
                    "rationale": str(item.get("rationale") or "").strip(),
                    "strategy": str(item.get("strategy") or "atomic_from_mermaid").strip(),
                    "files_involved": [
                        str(f).strip()
                        for f in list(item.get("files_involved") or [])
                        if str(f).strip()
                    ],
                }
            )
        return {"units": normalized}

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        units = parsed.get("units") or []
        if not units:
            return False, ["units list is empty"]
        for idx, unit in enumerate(units):
            if not isinstance(unit, dict):
                return False, [f"unit {idx} is not a dict"]
            if not unit.get("title") or not unit.get("objective"):
                return False, [f"unit {idx} missing title or objective"]
        return True, []

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        return None
