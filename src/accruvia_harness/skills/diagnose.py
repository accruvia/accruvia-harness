"""The /diagnose skill.

Replaces control_classifier.FailureClassifier. Takes failure evidence and
returns a structured classification: root cause, class, retry recommendation,
cooldown, scope adjustment. The LLM provides reasoning that a regex keyword
matcher cannot — it handles novel failures and sees the difference between
"test infrastructure broken" and "code defect broke the tests".

A deterministic keyword fast-path handles unambiguous infrastructure failures
(rate limits, credit exhaustion, provider outage) without an LLM call. The
skill falls back to the LLM for anything subtle.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..domain import FailureClassification
from .base import Skill, SkillResult, extract_json_payload, validate_against_schema


FAILURE_CLASSES = (
    "provider_rate_limit",
    "credit_exhaustion",
    "provider_outage",
    "timeout",
    "artifact_contract_failure",
    "hung_process",
    "code_defect",
    "test_infrastructure_failure",
    "scope_too_broad",
    "system_failure",
    "unknown",
)


@dataclass(slots=True)
class DiagnoseInputs:
    """Typed inputs for /diagnose."""

    evidence: str
    context: str = ""  # task objective, prior attempts, stage (work/validate/promote)
    attempt: int = 1
    prior_diagnoses: tuple[str, ...] = ()


# Deterministic fast-path: unambiguous failures that don't need an LLM.
_FAST_PATH_PATTERNS: tuple[tuple[tuple[str, ...], str, int], ...] = (
    # (keyword_substrings_all_required, classification, cooldown_seconds)
    (("rate limit",), "provider_rate_limit", 1800),
    (("429",), "provider_rate_limit", 1800),
    (("credit", "exhaust"), "credit_exhaustion", 0),
    (("credit", "insufficient"), "credit_exhaustion", 0),
    (("quota exceeded",), "credit_exhaustion", 0),
    (("connection refused",), "provider_outage", 1800),
    (("503", "service unavailable"), "provider_outage", 1800),
)


def _fast_path(evidence: str) -> FailureClassification | None:
    haystack = evidence.lower()
    for keywords, classification, cooldown in _FAST_PATH_PATTERNS:
        if all(kw in haystack for kw in keywords):
            retry = classification not in {"credit_exhaustion"}
            return FailureClassification(
                classification=classification,
                confidence=0.95,
                retry_recommended=retry,
                cooldown_seconds=cooldown,
                evidence=[evidence[:500]],
            )
    return None


class DiagnoseSkill:
    """LLM-backed failure diagnosis with structured classification output."""

    name = "diagnose"
    output_schema: dict[str, Any] = {
        "required": [
            "classification",
            "confidence",
            "retry_recommended",
            "cooldown_seconds",
            "root_cause",
        ],
        "types": {
            "classification": "str",
            "confidence": "float",
            "retry_recommended": "bool",
            "cooldown_seconds": "int",
            "root_cause": "str",
            "scope_adjustment": "str",
        },
        "allowed_values": {"classification": list(FAILURE_CLASSES)},
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        evidence = str(inputs.get("evidence") or "").strip()
        context = str(inputs.get("context") or "").strip()
        attempt = int(inputs.get("attempt") or 1)
        prior = list(inputs.get("prior_diagnoses") or [])
        classes_list = "\n".join(f"  - {name}" for name in FAILURE_CLASSES)
        prior_block = ""
        if prior:
            prior_block = (
                "Prior attempts were diagnosed as: "
                + ", ".join(prior)
                + ". If the same class keeps recurring, consider whether the "
                "problem is actually a different class that your earlier diagnoses missed."
            )
        return "\n\n".join(
            [
                "You are the failure diagnosis specialist for an LLM-software harness. "
                "Your job is to read failure evidence and classify it with high signal. "
                "You replace a regex keyword matcher — so look for subtle root causes it "
                "would miss, like code defects masquerading as test infrastructure failures, "
                "or task scope being too broad to complete in the available budget.",
                f"This is attempt {attempt} of the task.",
                "Return strict JSON with keys:\n"
                "  classification (string, one of the allowed values below)\n"
                "  confidence (float between 0.0 and 1.0)\n"
                "  retry_recommended (bool)\n"
                "  cooldown_seconds (int; 0 if retry can happen immediately)\n"
                "  root_cause (string, one sentence naming the actual underlying problem)\n"
                "  scope_adjustment (string; suggest narrowing/changing task scope if "
                "classification is code_defect or scope_too_broad; empty string otherwise)",
                f"Allowed classification values:\n{classes_list}",
                "Definitions:\n"
                "  code_defect: the code written by the harness is wrong (logic bug, type error, "
                "missing import). Distinct from test_infrastructure_failure.\n"
                "  test_infrastructure_failure: the test runner, fixtures, or environment is "
                "broken — not the code under test.\n"
                "  artifact_contract_failure: expected output files/fields are missing; the "
                "worker did not follow the output contract.\n"
                "  scope_too_broad: the task attempted more than a single run can deliver; "
                "classification should recommend splitting.\n"
                "  system_failure: an unexpected harness-level error that doesn't fit elsewhere.\n"
                "  unknown: reserved for cases where evidence is too sparse to classify.",
                "Retry guidance: rate limits and credit exhaustion should NOT retry quickly. "
                "Timeouts, artifact contract failures, and hung processes should retry. "
                "Code defects should retry with scope_adjustment guidance.",
                prior_block,
                "Context:\n" + (context or "(none)"),
                "Evidence:\n" + (evidence or "(no evidence provided)"),
            ]
        ).strip()

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        # Coerce numeric fields that LLMs may render as strings
        if "confidence" in parsed and isinstance(parsed["confidence"], str):
            try:
                parsed["confidence"] = float(parsed["confidence"])
            except ValueError:
                pass
        if "cooldown_seconds" in parsed and isinstance(parsed["cooldown_seconds"], (str, float)):
            try:
                parsed["cooldown_seconds"] = int(float(parsed["cooldown_seconds"]))
            except (ValueError, TypeError):
                pass
        # Normalize missing optional field
        if "scope_adjustment" not in parsed:
            parsed["scope_adjustment"] = ""
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if ok:
            confidence = parsed.get("confidence", 0)
            if not (0.0 <= float(confidence) <= 1.0):
                return False, [f"confidence out of range: {confidence}"]
            cooldown = parsed.get("cooldown_seconds", 0)
            if int(cooldown) < 0:
                return False, [f"cooldown_seconds must be >= 0: {cooldown}"]
        return ok, errors

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        # /diagnose is read-only. The caller applies classification policy.
        return None

    @staticmethod
    def to_classification(output: dict[str, Any], evidence: str) -> FailureClassification:
        """Adapt skill output to the domain FailureClassification dataclass."""
        return FailureClassification(
            classification=str(output.get("classification") or "unknown"),
            confidence=float(output.get("confidence") or 0.0),
            retry_recommended=bool(output.get("retry_recommended", False)),
            cooldown_seconds=int(output.get("cooldown_seconds") or 0),
            evidence=[evidence[:500]] if evidence else [],
        )

    @staticmethod
    def try_fast_path(evidence: str) -> FailureClassification | None:
        """Deterministic classification for unambiguous infrastructure failures."""
        return _fast_path(evidence or "")
