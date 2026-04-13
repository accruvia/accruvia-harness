"""Shared base for objective-review reviewer skills.

Every reviewer skill takes the same inputs and emits a single review
packet for one dimension. The dimension is encoded in the skill name as
``review_<dimension>``.
"""
from __future__ import annotations

import json
from typing import Any, ClassVar

from ..base import SkillResult, extract_json_payload, validate_against_schema


_VALID_VERDICTS = ("pass", "concern", "remediation_required")


class BaseReviewerSkill:
    """Abstract reviewer skill — subclasses set ``name`` and ``dimension``.

    Subclasses are expected to override ``dimension`` (matching the suffix of
    ``name``) and may override ``dimension_emphasis`` to add per-dimension
    guidance. ``build_prompt`` composes the shared envelope with the emphasis.
    """

    name: ClassVar[str] = "review_base"
    dimension: ClassVar[str] = "base"
    reviewer_label: ClassVar[str] = "objective_reviewer"
    dimension_emphasis: ClassVar[str] = ""

    output_schema: ClassVar[dict[str, Any]] = {
        "required": ["dimension", "verdict", "summary", "findings"],
        "types": {
            "dimension": "str",
            "verdict": "str",
            "summary": "str",
            "findings": "list",
        },
        "allowed_values": {"verdict": list(_VALID_VERDICTS)},
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        objective_title = str(inputs.get("objective_title") or "")
        objective_summary = str(inputs.get("objective_summary") or "")
        intent_summary = str(inputs.get("intent_summary") or "")
        success_definition = str(inputs.get("success_definition") or "")
        non_negotiables = list(inputs.get("non_negotiables") or [])
        mermaid_content = str(inputs.get("mermaid_content") or "")
        task_titles = list(inputs.get("task_titles") or [])
        changed_files = list(inputs.get("changed_files") or [])
        diff_text = str(inputs.get("diff_text") or "")
        prior_packet = inputs.get("prior_packet") if isinstance(inputs.get("prior_packet"), dict) else None

        emphasis_block = (self.dimension_emphasis or "").strip()
        emphasis_section = f"\nDimension emphasis ({self.dimension}):\n{emphasis_block}\n" if emphasis_block else ""
        prior_section = ""
        if prior_packet:
            prior_section = "\nPrior packet for this dimension:\n" + json.dumps(prior_packet, indent=2, sort_keys=True) + "\n"

        return (
            "You are a single-dimension objective reviewer for the accruvia harness.\n"
            f"Your assigned dimension is '{self.dimension}'. Stay strictly within this dimension.\n"
            "Examine the objective, the intent model, the accepted Mermaid, the linked tasks,\n"
            "and the diff/file change list. Issue ONE packet for this dimension.\n"
            "Verdict must be one of: pass, concern, remediation_required.\n"
            "Findings must be concrete, file/line-anchored when possible.\n"
            "Return JSON only with keys: dimension, verdict, summary, findings, evidence,\n"
            "severity, owner_scope, required_artifact_type, artifact_schema, closure_criteria, evidence_required.\n"
            "Use empty strings for fields that are not applicable when verdict is pass.\n"
            f"{emphasis_section}"
            f"{prior_section}"
            "\n"
            f"Objective title: {objective_title}\n"
            f"Objective summary: {objective_summary}\n"
            f"Intent summary: {intent_summary}\n"
            f"Success definition: {success_definition}\n"
            f"Non-negotiables: {json.dumps(non_negotiables)}\n"
            f"Accepted Mermaid:\n{mermaid_content}\n"
            f"Linked task titles: {json.dumps(task_titles)}\n"
            f"Changed files: {json.dumps(changed_files)}\n"
            f"Diff (truncated):\n{diff_text[:8000]}\n"
        )

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        # Normalise verdict casing
        verdict = str(parsed.get("verdict") or "").strip().lower()
        if verdict:
            parsed["verdict"] = verdict
        if "dimension" not in parsed or not str(parsed.get("dimension") or "").strip():
            parsed["dimension"] = self.dimension
        # Normalise findings to list[str]
        findings = parsed.get("findings")
        if isinstance(findings, list):
            parsed["findings"] = [str(item).strip() for item in findings if str(item).strip()]
        elif isinstance(findings, str):
            parsed["findings"] = [findings.strip()] if findings.strip() else []
        else:
            parsed["findings"] = []
        if "summary" not in parsed:
            parsed["summary"] = ""
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        if str(parsed.get("dimension") or "") != self.dimension:
            return False, [
                f"dimension must be '{self.dimension}', got '{parsed.get('dimension')}'"
            ]
        return True, []

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        return None
