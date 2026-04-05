"""The /scope skill — tech-lead perspective.

Reads a task (objective, strategy, allowed/forbidden paths) and repo context
and produces a concrete scope: which files will be touched, which must NOT
be touched, what the approach is, and what the risks are.

This skill runs FIRST in the work pipeline. Its output becomes the input
contract for /implement. Narrowing scope up front prevents the "worker writes
report.json and hopes" failure mode and gives the rest of the pipeline
something structured to validate against.
"""
from __future__ import annotations

from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


class ScopeSkill:
    """Narrow scope decision for a task, returning a structured plan."""

    name = "scope"
    output_schema: dict[str, Any] = {
        "required": ["files_to_touch", "approach", "risks"],
        "types": {
            "files_to_touch": "list",
            "files_not_to_touch": "list",
            "approach": "str",
            "risks": "list",
            "estimated_complexity": "str",
        },
        "allowed_values": {
            "estimated_complexity": ["trivial", "small", "medium", "large", "too_large"],
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        title = str(inputs.get("title") or "").strip()
        objective = str(inputs.get("objective") or "").strip()
        strategy = str(inputs.get("strategy") or "").strip()
        allowed = list(inputs.get("allowed_paths") or [])
        forbidden = list(inputs.get("forbidden_paths") or [])
        repo_context = str(inputs.get("repo_context") or "").strip()
        prior_scope = inputs.get("prior_scope")
        retry_feedback = str(inputs.get("retry_feedback") or "").strip()

        scope_constraints = []
        if allowed:
            scope_constraints.append(
                "Allowed paths (scope MUST stay within these):\n"
                + "\n".join(f"  - {p}" for p in allowed)
            )
        if forbidden:
            scope_constraints.append(
                "Forbidden paths (MUST NOT be touched):\n"
                + "\n".join(f"  - {p}" for p in forbidden)
            )

        prior_block = ""
        if prior_scope or retry_feedback:
            prior_block = (
                "This is a retry. Prior scope and feedback follow. Adjust the scope "
                "based on what went wrong — narrow the blast radius, split if the "
                "task is too big, or change files_to_touch if the prior set was wrong.\n"
            )
            if prior_scope:
                prior_block += f"Prior scope: {prior_scope}\n"
            if retry_feedback:
                prior_block += f"Retry feedback: {retry_feedback}\n"

        return "\n\n".join(
            filter(
                None,
                [
                    "You are the tech lead scoping a single software task. Your job is "
                    "to decide EXACTLY which files need to be touched to satisfy the "
                    "objective, what approach to take, and what the risks are. Narrow "
                    "scope is a virtue. Broad scope is a risk.",
                    f"Task title: {title}",
                    f"Task objective: {objective}",
                    f"Strategy: {strategy or '(none specified)'}",
                    "\n\n".join(scope_constraints) if scope_constraints else "",
                    "Repository context (summary of relevant files, current state):\n"
                    + (repo_context or "(none provided)"),
                    prior_block,
                    "Return strict JSON with keys:\n"
                    "  files_to_touch (list of relative file paths the implementer will edit/create)\n"
                    "  files_not_to_touch (list of files the implementer MUST leave alone; "
                    "can be empty; include files the reader might assume are in scope but "
                    "must not be changed)\n"
                    "  approach (string, 2-5 sentences describing what will change and why)\n"
                    "  risks (list of strings naming concrete risks of this scope)\n"
                    "  estimated_complexity (one of: trivial, small, medium, large, too_large)",
                    "If estimated_complexity is 'too_large', explain in approach why the "
                    "task should be split before implementation. Set files_to_touch to the "
                    "minimum viable first slice in that case.",
                ],
            )
        ).strip()

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        # Normalize: ensure list-typed fields exist
        parsed.setdefault("files_not_to_touch", [])
        parsed.setdefault("estimated_complexity", "medium")
        # Coerce string paths to strings if LLM returned dicts
        for key in ("files_to_touch", "files_not_to_touch"):
            if isinstance(parsed.get(key), list):
                parsed[key] = [
                    str(item.get("path") if isinstance(item, dict) else item)
                    for item in parsed[key]
                    if item
                ]
        if isinstance(parsed.get("risks"), list):
            parsed["risks"] = [str(r) for r in parsed["risks"] if r]
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if ok:
            if not parsed.get("files_to_touch"):
                return False, ["files_to_touch must be non-empty"]
            if not parsed.get("approach", "").strip():
                return False, ["approach must be non-empty"]
        return ok, errors

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        # /scope is read-only: the orchestrator records the scope as an artifact
        # on the run, not directly in DB. See work-phase orchestration.
        return None
