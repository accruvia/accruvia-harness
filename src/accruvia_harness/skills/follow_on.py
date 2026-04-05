"""The /follow-on skill — generates task proposals from rejection reasons.

When /promotion-review rejects a candidate, or post-merge-check recommends
rollback, we need to generate concrete follow-on tasks. This skill uses the
SAME schema as CognitionService.heartbeat() proposed_tasks[], so the existing
task materialization layer handles it with no new code path.

That's the payoff of schema unification: planning, review rejection, and
post-merge rollback all feed into the same task creation funnel.
"""
from __future__ import annotations

from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


class FollowOnSkill:
    """Emits proposed_tasks[] in cognition's schema from rejection context."""

    name = "follow_on"
    output_schema: dict[str, Any] = {
        "required": ["proposed_tasks", "summary"],
        "types": {
            "proposed_tasks": "list",
            "summary": "str",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        original_title = str(inputs.get("original_title") or "").strip()
        original_objective = str(inputs.get("original_objective") or "").strip()
        rejection_reason = str(inputs.get("rejection_reason") or "").strip()
        concerns = list(inputs.get("concerns") or [])
        rollback_reason = str(inputs.get("rollback_reason") or "").strip()
        stage = str(inputs.get("stage") or "review_rejection").strip()

        concerns_block = ""
        if concerns:
            concerns_block = "Reviewer concerns to address:\n" + "\n".join(
                f"  - {c}" for c in concerns
            )

        rollback_block = ""
        if rollback_reason:
            rollback_block = (
                "A post-merge rollback was triggered. The follow-on MUST repair the "
                "specific failure:\n" + rollback_reason
            )

        return "\n\n".join(
            filter(
                None,
                [
                    "You are splitting a rejected task into concrete follow-on tasks. "
                    "Each follow-on must be narrow, atomic, and directly address a "
                    "specific concern or failure. Reuse the same schema as the "
                    "planning heartbeat.",
                    f"Original task title: {original_title}",
                    f"Original objective: {original_objective}",
                    f"Stage where rejection occurred: {stage}",
                    f"Rejection reason: {rejection_reason}" if rejection_reason else "",
                    concerns_block,
                    rollback_block,
                    "Return strict JSON with keys:\n"
                    "  proposed_tasks (list; each item has required keys "
                    "'title', 'objective', 'priority' [P0|P1|P2|P3], 'rationale'; "
                    "optional keys 'allowed_paths', 'forbidden_paths', 'strategy', "
                    "'validation_profile')\n"
                    "  summary (one sentence explaining what was split and why)",
                    "Generate 1-3 follow-on tasks. Fewer is better if the fix is "
                    "atomic. Prefer high priority (P1 or P0) for follow-ons since "
                    "they unblock a rejected promotion.",
                ],
            )
        ).strip()

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        parsed.setdefault("summary", "")
        if isinstance(parsed.get("proposed_tasks"), list):
            normalized: list[dict[str, Any]] = []
            for item in parsed["proposed_tasks"]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                objective = str(item.get("objective") or "").strip()
                if not title or not objective:
                    continue
                entry: dict[str, Any] = {
                    "title": title,
                    "objective": objective,
                    "priority": str(item.get("priority") or "P2"),
                    "rationale": str(item.get("rationale") or "").strip(),
                }
                for opt_key in ("allowed_paths", "forbidden_paths"):
                    if isinstance(item.get(opt_key), list):
                        entry[opt_key] = [str(p) for p in item[opt_key] if p]
                if item.get("strategy"):
                    entry["strategy"] = str(item["strategy"])
                if item.get("validation_profile"):
                    entry["validation_profile"] = str(item["validation_profile"])
                normalized.append(entry)
            parsed["proposed_tasks"] = normalized
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        tasks = parsed.get("proposed_tasks") or []
        if not tasks:
            return False, ["proposed_tasks must contain at least one task"]
        return True, []

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        # Delegation: the orchestrator passes this output to CognitionService's
        # _materialize_proposed_tasks, which already handles dedup, scope,
        # priority parsing, and event emission. No new code needed.
        return None
