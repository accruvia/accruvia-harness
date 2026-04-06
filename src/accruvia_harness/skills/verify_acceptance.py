"""The /verify-acceptance skill — check implementation against plain-English criteria.

After the pipeline completes, this skill takes the original acceptance
criteria (from /translate-intent) and the implementation evidence (diff,
changed files, test results, quality report) and produces a structured
verdict: each criterion → met / not-met / unclear, with evidence.

This replaces "read the diff" for non-developers. They get a checklist
they can understand and act on.
"""
from __future__ import annotations

from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


class VerifyAcceptanceSkill:
    """Check implementation against plain-English acceptance criteria."""

    name = "verify_acceptance"
    output_schema: dict[str, Any] = {
        "required": ["criteria_results", "all_met", "summary_for_requester"],
        "types": {
            "criteria_results": "list",
            "all_met": "bool",
            "summary_for_requester": "str",
            "next_steps": "list",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        intent = str(inputs.get("intent") or "").strip()
        acceptance_criteria = list(inputs.get("acceptance_criteria") or [])
        diff = str(inputs.get("diff") or "").strip()
        changed_files = list(inputs.get("changed_files") or [])
        test_results = str(inputs.get("test_results") or "").strip()
        quality_summary = str(inputs.get("quality_summary") or "").strip()
        implementation_rationale = str(inputs.get("implementation_rationale") or "").strip()

        criteria_block = "\n".join(
            f"  {i+1}. {c}" for i, c in enumerate(acceptance_criteria)
        ) if acceptance_criteria else "  (no acceptance criteria provided)"

        return "\n\n".join(
            filter(
                None,
                [
                    "You are verifying whether a code change satisfies the requester's "
                    "acceptance criteria. The requester is NOT a developer — they stated "
                    "what they wanted in plain English, and now they need to know: did "
                    "they get it?",
                    f"Original request: {intent}",
                    f"Acceptance criteria:\n{criteria_block}",
                    f"What was implemented: {implementation_rationale}",
                    f"Changed files: {', '.join(changed_files) if changed_files else '(none)'}",
                    f"Test results: {test_results or '(not available)'}",
                    f"Quality summary: {quality_summary or '(not available)'}",
                    "Implementation diff:\n" + (diff[:10000] or "(no diff available)"),
                    "Return strict JSON with keys:\n"
                    "  criteria_results (list of objects, each with:\n"
                    "    'criterion' (the original text),\n"
                    "    'status' ('met', 'not_met', or 'unclear'),\n"
                    "    'evidence' (string explaining WHY, in plain language))\n"
                    "  all_met (bool — true only if every criterion is 'met')\n"
                    "  summary_for_requester (2-3 sentences in plain language: what "
                    "was built, whether it meets expectations, and any caveats)\n"
                    "  next_steps (list of strings: what the requester should do "
                    "next — e.g. 'Try saving your preferences and reloading the page')",
                    "RULES:\n"
                    "  - Judge ONLY against the acceptance criteria. Don't add new ones.\n"
                    "  - 'met' = the diff clearly implements this. Evidence should point "
                    "to specific changes.\n"
                    "  - 'not_met' = the diff does NOT implement this, or actively "
                    "contradicts it.\n"
                    "  - 'unclear' = can't determine from the diff alone (e.g. requires "
                    "runtime testing the requester should do).\n"
                    "  - summary_for_requester should avoid ALL jargon. Write as if "
                    "explaining to someone who has never seen code.\n"
                    "  - next_steps should be concrete actions, not 'review the changes'.",
                ],
            )
        ).strip()

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        parsed.setdefault("next_steps", [])
        if isinstance(parsed.get("criteria_results"), list):
            normalized: list[dict[str, str]] = []
            for item in parsed["criteria_results"]:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status") or "unclear").lower()
                if status not in ("met", "not_met", "unclear"):
                    status = "unclear"
                normalized.append({
                    "criterion": str(item.get("criterion") or ""),
                    "status": status,
                    "evidence": str(item.get("evidence") or ""),
                })
            parsed["criteria_results"] = normalized
        sr = parsed.get("all_met")
        if isinstance(sr, str):
            parsed["all_met"] = sr.strip().lower() in ("true", "yes", "1")
        if isinstance(parsed.get("next_steps"), list):
            parsed["next_steps"] = [str(s) for s in parsed["next_steps"] if s]
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        if not parsed.get("criteria_results"):
            return False, ["criteria_results must have at least one entry"]
        if not parsed.get("summary_for_requester", "").strip():
            return False, ["summary_for_requester must be non-empty"]
        # Self-consistency: all_met must be False if any criterion is not_met
        has_not_met = any(
            c.get("status") == "not_met"
            for c in parsed.get("criteria_results") or []
        )
        if has_not_met and parsed.get("all_met"):
            parsed["all_met"] = False
        return True, []

    def materialize(self, store: Any, result: SkillResult, inputs: dict[str, Any]) -> None:
        return None
