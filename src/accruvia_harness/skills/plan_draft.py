"""Plan draft skill — produces structured plan decomposition from intent.

This skill replaces `atomic_decomposition` as the source of plan rows in
the new Model C flow. The key differences:

  - `atomic_decomposition` consumed a finished Mermaid and produced task
    units (keyed to positional node ids that never matched anything).
  - `plan_draft` consumes the intent model + interrogation output and
    produces structured plans directly. Each plan has an ephemeral
    `local_id` (p1, p2, ...) that dependencies reference. At materialize
    time the caller converts local_ids to canonical plan.ids and writes
    Plan rows to the store; the Mermaid is rendered from the plans via
    `mermaid.render_mermaid_from_plans`.

Invariant enforced by validate_output:
  - depends_on can only reference *earlier* plans in the list (no forward
    references, no cycles, no self-references)
  - local_ids are unique within the list
  - every plan has a non-empty label
  - soft cap: <= 15 plans per objective (larger decompositions almost
    always indicate the objective is not really atomic yet)

The skill itself is a pure function of its inputs; persistence lives in
`materialize_plans_from_skill_output`, a helper in this module that
converts validated output into real Plan rows. This keeps the skill
stateless and testable without a real store.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..domain import Plan, new_id
from ..mermaid import canonical_node_id
from .base import SkillResult, extract_json_payload, validate_against_schema


_MAX_PLANS = 15


class PlanDraftSkill:
    name = "plan_draft"
    output_schema: dict[str, Any] = {
        "required": ["plans"],
        "types": {"plans": "list"},
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        objective_title = str(inputs.get("objective_title") or "")
        objective_summary = str(inputs.get("objective_summary") or "")
        intent_summary = str(inputs.get("intent_summary") or "")
        success_definition = str(inputs.get("success_definition") or "")
        non_negotiables = list(inputs.get("non_negotiables") or [])
        frustration_signals = list(inputs.get("frustration_signals") or [])
        interrogation_questions = list(inputs.get("interrogation_questions") or [])
        interrogation_red_team = list(inputs.get("interrogation_red_team_findings") or [])
        prior_round_findings = [
            str(x).strip()
            for x in list(inputs.get("prior_round_findings") or [])
            if str(x).strip()
        ]
        round_number = int(inputs.get("round_number") or 1)

        prior_block = ""
        if prior_round_findings:
            prior_block = (
                f"\nThis is plan_draft round {round_number}. The previous candidate failed "
                "the following red-team findings. Rework the plans so each objection is "
                "addressed directly — do not repeat the same mistakes.\n"
                f"Prior-round findings:\n{json.dumps(prior_round_findings, indent=2)}\n"
            )

        return (
            "You are decomposing a software objective into an ordered sequence of atomic plans.\n\n"
            "DEFINITION OF ATOMIC:\n"
            "An atomic plan represents ONE coherent change — typically a single implementation file\n"
            "plus its dedicated test plus a CHANGELOG entry. It is the smallest reviewable diff.\n"
            "A plan must be something a single commit can deliver end-to-end.\n\n"
            "TARGET SIZE (empirical, from the existing corpus of real atomic commits):\n"
            "  - median commit: 3 files changed, ~65 lines total\n"
            "  - p90: 3 files, 250 lines\n"
            "  - pattern: 1 impl file + 1 test file + 1 CHANGELOG line\n"
            "  - typical objective decomposes into 5-12 plans\n"
            f"  - HARD CAP: never exceed {_MAX_PLANS} plans per objective\n\n"
            "OUTPUT FORMAT (JSON only):\n"
            "{\n"
            '  "plans": [\n'
            '    {"local_id": "p1", "label": "...", "depends_on": []},\n'
            '    {"local_id": "p2", "label": "...", "depends_on": ["p1"]},\n'
            "    ...\n"
            "  ]\n"
            "}\n\n"
            "Each plan MUST have:\n"
            "  - local_id: unique within the list; use p1, p2, p3, ... in order\n"
            "  - label: 8-25 words in imperative voice describing the concrete change.\n"
            "    Name specific files, functions, classes, or tests where possible.\n"
            '    Good: "Add run_phase field and RunPhase enum to domain.Run"\n'
            '    Bad: "Improve run execution"\n'
            "  - depends_on: list of local_ids of plans that must complete before this one.\n"
            "    May only reference EARLIER plans in the list. No forward references, no cycles.\n\n"
            "CONSTRAINTS:\n"
            "  - Every non-negotiable must be addressed by at least one plan.\n"
            "  - The first plan (p1) has depends_on = [].\n"
            "  - Dependencies must reflect real ordering constraints, not preferences.\n"
            "  - No two plans should describe the same concern.\n"
            "  - Group related impl + test work into a single plan; do not split impl and test\n"
            "    into separate plans unless the test covers code not introduced by the plan.\n"
            "  - The last plan is often a test-sweep or integration-verify step; this is fine.\n\n"
            f"OBJECTIVE TITLE: {objective_title}\n"
            f"OBJECTIVE SUMMARY: {objective_summary}\n"
            f"INTENT SUMMARY: {intent_summary}\n"
            f"SUCCESS DEFINITION: {success_definition}\n"
            f"NON-NEGOTIABLES: {json.dumps(non_negotiables, indent=2)}\n"
            f"FRUSTRATION SIGNALS: {json.dumps(frustration_signals, indent=2)}\n"
            f"INTERROGATION QUESTIONS: {json.dumps(interrogation_questions, indent=2)}\n"
            f"INTERROGATION RED-TEAM FINDINGS: {json.dumps(interrogation_red_team, indent=2)}\n"
            f"{prior_block}"
            "\nReturn JSON only."
        )

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {"plans": []}
        raw = parsed.get("plans")
        if not isinstance(raw, list):
            return {"plans": []}
        normalized: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            local_id = str(item.get("local_id") or "").strip()
            label = str(item.get("label") or "").strip()
            deps_raw = item.get("depends_on") or item.get("dependencies") or []
            if isinstance(deps_raw, list):
                depends_on = [str(d).strip() for d in deps_raw if str(d).strip()]
            else:
                depends_on = []
            if local_id and label:
                normalized.append(
                    {"local_id": local_id, "label": label, "depends_on": depends_on}
                )
        return {"plans": normalized}

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return False, errors
        plans = parsed.get("plans") or []
        if not plans:
            return False, ["plans list is empty"]
        if len(plans) > _MAX_PLANS:
            return False, [
                f"plans list has {len(plans)} entries, exceeds max of {_MAX_PLANS}"
            ]

        seen_ids: set[str] = set()
        for idx, plan in enumerate(plans):
            if not isinstance(plan, dict):
                return False, [f"plan {idx} is not a dict"]
            local_id = str(plan.get("local_id") or "").strip()
            if not local_id:
                return False, [f"plan {idx} missing local_id"]
            if local_id in seen_ids:
                return False, [f"duplicate local_id: {local_id!r}"]
            seen_ids.add(local_id)
            if not str(plan.get("label") or "").strip():
                return False, [f"plan {local_id!r} has empty label"]

        # Forward-reference / cycle / self-reference check
        seen_so_far: set[str] = set()
        for plan in plans:
            local_id = plan["local_id"]
            deps = plan.get("depends_on") or []
            for dep in deps:
                if dep == local_id:
                    return False, [f"self-reference: {local_id!r} depends on itself"]
                if dep not in seen_so_far:
                    return False, [
                        f"forward or unknown reference: plan {local_id!r} "
                        f"depends_on {dep!r} which is not an earlier plan"
                    ]
            seen_so_far.add(local_id)

        return True, []

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        """No-op: plan materialization lives in a separate helper so it can
        be called out-of-band (e.g. from eval scripts that want to inspect
        the skill output before persisting). Use
        `materialize_plans_from_skill_output` when you actually want to
        write Plan rows to the store.
        """
        return None


def materialize_plans_from_skill_output(
    store: Any,
    objective_id: str,
    plans_data: list[dict[str, Any]],
    *,
    author_tag: str = "plan_draft_skill",
) -> list[Plan]:
    """Convert plan_draft skill output into real Plan rows.

    - Assigns canonical `P_<hash>` ids via `mermaid.canonical_node_id`.
    - Resolves `depends_on` local_ids to canonical plan.ids (plans are
      created in order, so earlier local_ids have real ids by the time
      later plans reference them).
    - Persists each plan via `store.create_plan`.

    Returns the list of persisted Plan rows in creation order.
    """
    base_ts = datetime.now(tz=timezone.utc)
    local_to_plan_id: dict[str, str] = {}
    persisted: list[Plan] = []

    for idx, pd in enumerate(plans_data):
        local_id = pd["local_id"]
        label = pd["label"]
        deps_local = pd.get("depends_on") or []
        deps_resolved = [
            local_to_plan_id[d] for d in deps_local if d in local_to_plan_id
        ]
        plan = Plan(
            id=new_id("plan"),
            objective_id=objective_id,
            slice={
                "label": label,
                "dependencies": deps_resolved,
                "derived_from": author_tag,
                "local_id": local_id,
            },
            atomicity_assessment={
                "is_atomic": True,
                "violations": [],
                "reason": f"from {author_tag}",
            },
            approval_status="approved",
            created_at=base_ts + timedelta(seconds=idx),
            updated_at=base_ts + timedelta(seconds=idx),
        )
        plan.mermaid_node_id = canonical_node_id(plan)
        store.create_plan(plan)
        local_to_plan_id[local_id] = plan.id
        persisted.append(plan)

    return persisted
