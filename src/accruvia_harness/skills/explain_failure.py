"""The /explain-failure skill — translate technical failures into plain language.

When a task fails, the harness has rich diagnostic data: failure_category,
failure_message, diagnosis, stage, validation output. But a non-developer
sees "Retry budget exhausted" and has no idea what happened or what to do.

This skill reads the diagnostics and produces:
  - what_happened: plain-English explanation (no jargon)
  - why_it_matters: impact on the requester's goal
  - what_to_try: concrete next actions the requester can take
  - can_retry: bool — whether trying again might help

It's called from the `request` CLI when the task doesn't complete.
"""
from __future__ import annotations

from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


# Map internal failure categories to plain-language explanations.
# Used as fallback when the LLM isn't available.
_FALLBACK_EXPLANATIONS: dict[str, dict[str, str]] = {
    "provider_rate_limit": {
        "what_happened": "The AI service is temporarily overloaded. Too many requests are being processed right now.",
        "what_to_try": "Wait 30 minutes and try again. This usually resolves on its own.",
    },
    "credit_exhaustion": {
        "what_happened": "The AI service has run out of credits. No more requests can be processed until credits are added.",
        "what_to_try": "Contact the system administrator to add credits to the AI account.",
    },
    "scope_too_broad": {
        "what_happened": "Your request is too large to build in one step. It needs to be broken into smaller pieces.",
        "what_to_try": "Try asking for a smaller part of what you want. For example, instead of 'build a complete dashboard', try 'add a chart showing daily usage'.",
    },
    "timeout": {
        "what_happened": "The system took too long to complete your request and had to stop.",
        "what_to_try": "Try a simpler version of your request, or try again — sometimes the AI needs a second attempt.",
    },
    "scope_skill_failure": {
        "what_happened": "The system couldn't figure out which parts of the code to change for your request.",
        "what_to_try": "Try rephrasing your request with more specific details about what you want to happen.",
    },
    "validation_failure": {
        "what_happened": "The code was written but didn't pass quality checks. The automated tests found problems.",
        "what_to_try": "Try again — the system will attempt a different approach. If it keeps failing, the request may need a developer's help.",
    },
}


class ExplainFailureSkill:
    """Translate technical failure diagnostics into plain language."""

    name = "explain_failure"
    output_schema: dict[str, Any] = {
        "required": ["what_happened", "why_it_matters", "what_to_try", "can_retry"],
        "types": {
            "what_happened": "str",
            "why_it_matters": "str",
            "what_to_try": "list",
            "can_retry": "bool",
            "technical_detail": "str",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        intent = str(inputs.get("intent") or "").strip()
        failure_category = str(inputs.get("failure_category") or "").strip()
        failure_message = str(inputs.get("failure_message") or "").strip()
        stage = str(inputs.get("stage") or "").strip()
        diagnosis = inputs.get("diagnosis") or {}
        attempt_count = int(inputs.get("attempt_count") or 1)
        max_attempts = int(inputs.get("max_attempts") or 2)

        return "\n\n".join(
            filter(
                None,
                [
                    "You are explaining a technical failure to someone who has NEVER "
                    "seen code. They asked for a feature and it couldn't be built. "
                    "Explain what happened, why it matters to them, and what they "
                    "can do about it. ZERO jargon. Write like you're talking to a "
                    "friend who asked you to help with their computer.",
                    f"Their request was: {intent}",
                    f"What failed (technical): stage={stage}, category={failure_category}",
                    f"Technical error message: {failure_message[:500]}",
                    f"Diagnosis: {diagnosis}" if diagnosis else "",
                    f"Attempts: {attempt_count} of {max_attempts} used",
                    "Return strict JSON with keys:\n"
                    "  what_happened (string, 1-2 sentences, NO jargon)\n"
                    "  why_it_matters (string, 1 sentence: what this means for their request)\n"
                    "  what_to_try (list of strings: concrete actions they can take)\n"
                    "  can_retry (bool: would trying again with the same request help?)\n"
                    "  technical_detail (string: one sentence of technical context "
                    "for a developer if one gets involved, OK to use jargon here)",
                    "GUIDELINES:\n"
                    "  - 'what_happened' must make sense to someone who doesn't know "
                    "what code, tests, or APIs are.\n"
                    "  - 'what_to_try' should be things THEY can do, not things a "
                    "developer would do. 'Wait and try again', 'Ask for a smaller "
                    "feature', 'Contact support' — not 'check the logs'.\n"
                    "  - If can_retry is True, the first item in what_to_try should "
                    "be 'Try your request again — the system will attempt a different approach.'\n"
                    "  - Be honest. If this is a system problem and not their fault, say so.",
                ],
            )
        ).strip()

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        parsed.setdefault("technical_detail", "")
        cr = parsed.get("can_retry")
        if isinstance(cr, str):
            parsed["can_retry"] = cr.strip().lower() in ("true", "yes", "1")
        if isinstance(parsed.get("what_to_try"), list):
            parsed["what_to_try"] = [str(s) for s in parsed["what_to_try"] if s]
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        if not parsed.get("what_happened", "").strip():
            return False, ["what_happened must be non-empty"]
        if not parsed.get("what_to_try"):
            return False, ["what_to_try must have at least one suggestion"]
        return True, []

    def materialize(self, store: Any, result: SkillResult, inputs: dict[str, Any]) -> None:
        return None

    @staticmethod
    def fallback_explanation(failure_category: str) -> dict[str, Any]:
        """Deterministic fallback when LLM is unavailable."""
        entry = _FALLBACK_EXPLANATIONS.get(failure_category, {
            "what_happened": "Something went wrong while building your request. The system encountered an unexpected problem.",
            "what_to_try": "Try your request again. If it keeps failing, contact support.",
        })
        return {
            "what_happened": entry.get("what_happened", "An unexpected error occurred."),
            "why_it_matters": "Your requested feature was not built.",
            "what_to_try": [entry.get("what_to_try", "Try again or contact support.")],
            "can_retry": failure_category in {"timeout", "provider_rate_limit", "validation_failure"},
            "technical_detail": f"failure_category={failure_category}",
        }
