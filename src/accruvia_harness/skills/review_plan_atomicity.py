"""Plan atomicity reviewer skill.

Critiques a TRIO-structured plan list produced by `plan_draft_trio` for
semantic atomicity issues that schema validation cannot catch. Designed
to run as the reviewer arm of `RedTeamLoopOrchestrator` wrapping
`plan_draft_trio`:

    orchestrator.execute(
        generator_skill_name="plan_draft_trio",
        reviewer_skill_names=["review_plan_atomicity"],
        stopping_predicate=lambda output, reviewer_results, round_number: (
            reviewer_results.get("review_plan_atomicity")
            and reviewer_results["review_plan_atomicity"].success
            and reviewer_results["review_plan_atomicity"].output.get("verdict") == "pass"
        ),
        ...
    )

The reviewer's findings are aggregated by `default_findings_extractor` and
threaded back into the generator's `prior_round_findings` input for the
next round.

Checks (LLM-judged, not mechanical):
    - transformation-to-target coherence: does each plan's `transformation`
      describe something that would plausibly land in `target_impl`?
    - sample realism: are `input_samples` shaped like what the target
      function/test would actually receive?
    - cross-plan file sprawl: do two plans touch related files without a
      dependency edge between them?
    - duplication: do two plans describe the same work under different
      labels?
    - non-negotiable completeness: is every non-negotiable from the intent
      model addressed by at least one plan?
    - size sanity: is any plan's transformation too large to be a single
      reviewable commit?
    - edge case coverage: for plans whose non-negotiables imply edge cases
      (size limits, null handling, concurrent access), is there >1 sample?
"""
from __future__ import annotations

import json
from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


_VALID_VERDICTS = ("pass", "concern", "remediation_required")
_VALID_SEVERITIES = ("critical", "major", "minor")


class ReviewPlanAtomicitySkill:
    name = "review_plan_atomicity"
    output_schema: dict[str, Any] = {
        "required": ["verdict", "summary", "findings"],
        "types": {
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
        generator_output = inputs.get("generator_output") or {}
        plans = generator_output.get("plans") if isinstance(generator_output, dict) else None
        plans = plans or []

        plans_json = json.dumps(plans, indent=2, sort_keys=False)
        # Truncate extremely long plan blocks to keep prompt token cost bounded
        if len(plans_json) > 12000:
            plans_json = plans_json[:12000] + "\n... (truncated for prompt budget) ..."

        return (
            "You are reviewing a TRIO-structured plan decomposition for semantic atomicity.\n"
            "The generator already passed schema validation — do NOT complain about missing\n"
            "fields, duplicate local_ids, or forward references. Your job is the semantic\n"
            "critique that schema checks cannot catch.\n\n"
            "CHECKS YOU MUST RUN (in order):\n\n"
            "1. TRANSFORMATION-TO-TARGET COHERENCE: for each plan, does its `transformation`\n"
            "   describe something that would plausibly land in its `target_impl`? If the\n"
            "   transformation says 'fix the bug in bar.py' but target_impl is 'src/foo.py',\n"
            "   that is a MAJOR finding.\n\n"
            "2. CROSS-PLAN FILE SPRAWL: do any two plans share a target_impl *file* (same\n"
            "   path before the `::` symbol) without one depending on the other? Two plans\n"
            "   that independently touch the same file and don't depend on each other are\n"
            "   sprawl — they should be merged, or one should depend on the other. MAJOR.\n\n"
            "3. DUPLICATION: do any two plans describe substantively the same work under\n"
            "   different labels? MAJOR.\n\n"
            "4. NON-NEGOTIABLE COMPLETENESS: is every non-negotiable from the intent model\n"
            "   addressed by at least one plan's transformation + target? If a non-negotiable\n"
            "   is orphaned (no plan covers it), that is a CRITICAL finding.\n\n"
            "5. SIZE SANITY: is any single plan's transformation too large to be one\n"
            "   reviewable commit? Empirical target is ~3 files, ~65 lines, impl+test+changelog.\n"
            "   If a plan's transformation says 'rewrite the entire X subsystem', that is a\n"
            "   MAJOR finding — split it.\n\n"
            "6. SAMPLE REALISM: for each plan, are `input_samples` shaped like real inputs\n"
            "   the target function/test would see? Are types and keys plausible given the\n"
            "   target_impl path? If samples are obviously placeholder or type-mismatched,\n"
            "   that is a MINOR finding per plan.\n\n"
            "7. EDGE CASE COVERAGE: for plans whose non-negotiables imply edge cases (size\n"
            "   limits, null handling, concurrent access, malformed input), is there >1\n"
            "   input_sample? If the non-negotiable says 'must handle null', a plan with\n"
            "   only happy-path samples is a MINOR finding.\n\n"
            "OUTPUT FORMAT (JSON only):\n"
            "{\n"
            '  "dimension": "plan_atomicity",\n'
            '  "verdict": "pass" | "concern" | "remediation_required",\n'
            '  "summary": "<1-3 sentence overall assessment>",\n'
            '  "findings": [\n'
            "    {\n"
            '      "plan_local_id": "p3",\n'
            '      "severity": "critical" | "major" | "minor",\n'
            '      "class": "<one of: sprawl, duplication, incoherent, orphan_non_negotiable,'
            ' oversized, thin_coverage, unrealistic_samples>",\n'
            '      "finding": "<concise description of the problem>",\n'
            '      "suggested_fix": "<concrete action the generator can take next round>"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "VERDICT RULES:\n"
            '  - "pass": no critical or major findings. Optionally minor ones. Generator\n'
            "    has already produced an atomic decomposition that ships as-is.\n"
            '  - "concern": 1+ major findings but no critical. Generator should fix next\n'
            "    round. Red-team loop continues.\n"
            '  - "remediation_required": 1+ critical findings (orphaned non-negotiable,\n'
            "    fundamentally wrong decomposition). Generator MUST address before continuing.\n\n"
            "Be strict on critical/major, lenient on minor. Do not invent findings to look\n"
            "useful — if the decomposition is good, return pass with empty findings.\n\n"
            f"OBJECTIVE TITLE: {objective_title}\n"
            f"OBJECTIVE SUMMARY: {objective_summary}\n"
            f"INTENT SUMMARY: {intent_summary}\n"
            f"SUCCESS DEFINITION: {success_definition}\n"
            f"NON-NEGOTIABLES: {json.dumps(non_negotiables, indent=2)}\n\n"
            f"PLAN LIST TO REVIEW:\n{plans_json}\n\n"
            "Return JSON only."
        )

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        out: dict[str, Any] = {
            "dimension": "plan_atomicity",
            "verdict": str(parsed.get("verdict") or "").strip().lower(),
            "summary": str(parsed.get("summary") or "").strip(),
            "findings": [],
        }
        raw_findings = parsed.get("findings") or []
        if isinstance(raw_findings, list):
            for item in raw_findings:
                if not isinstance(item, dict):
                    continue
                plan_local_id = str(item.get("plan_local_id") or item.get("local_id") or "").strip()
                severity = str(item.get("severity") or "").strip().lower()
                finding_text = str(item.get("finding") or "").strip()
                if not finding_text:
                    continue
                out["findings"].append(
                    {
                        "plan_local_id": plan_local_id,
                        "severity": severity if severity in _VALID_SEVERITIES else "minor",
                        "class": str(item.get("class") or "").strip(),
                        "finding": finding_text,
                        "suggested_fix": str(item.get("suggested_fix") or "").strip(),
                    }
                )
        return out

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return False, errors
        verdict = parsed.get("verdict")
        summary = parsed.get("summary") or ""
        findings = parsed.get("findings") or []

        if not summary.strip():
            return False, ["summary must be a non-empty string"]

        # Verdict/severity consistency: remediation_required requires at least
        # one critical finding; concern requires at least one major or critical;
        # pass must have no critical or major findings.
        severities = [f.get("severity", "") for f in findings]
        has_critical = "critical" in severities
        has_major = "major" in severities

        if verdict == "pass" and (has_critical or has_major):
            return False, [
                f"verdict=pass is inconsistent with severities {severities}: "
                "pass requires no critical or major findings"
            ]
        if verdict == "remediation_required" and not has_critical:
            return False, [
                "verdict=remediation_required requires at least one critical finding"
            ]
        if verdict == "concern" and not (has_critical or has_major):
            return False, [
                "verdict=concern requires at least one major or critical finding; "
                "use verdict=pass if all findings are minor"
            ]

        return True, []

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        return None
