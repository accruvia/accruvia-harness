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
from typing import TYPE_CHECKING, Any

from ..domain import Plan, new_id
from ..mermaid import canonical_node_id
from .base import SkillResult, extract_json_payload, validate_against_schema

if TYPE_CHECKING:
    from .context import SkillContext


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


_TRIO_SLICE_KEYS = (
    "target_impl",
    "target_test",
    "transformation",
    "input_samples",
    "output_samples",
    "resources",
    "supersedes",
    "orphan_strategy",
    "orphan_acceptance_reason",
)


def materialize_plans_from_skill_output(
    store: Any,
    objective_id: str,
    plans_data: list[dict[str, Any]],
    *,
    author_tag: str = "plan_draft_skill",
) -> list[Plan]:
    """Convert plan_draft (or plan_draft_trio) skill output into real Plan rows.

    - Assigns canonical `P_<hash>` ids via `mermaid.canonical_node_id`.
    - Resolves `depends_on` local_ids to canonical plan.ids (plans are
      created in order, so earlier local_ids have real ids by the time
      later plans reference them).
    - Copies TRIO fields (target_impl, target_test, transformation,
      input_samples, output_samples, resources) into plan.slice when
      present in the source data. Missing TRIO fields are simply absent
      from the slice dict — downstream consumers must defensively check.
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
        slice_dict: dict[str, Any] = {
            "label": label,
            "dependencies": deps_resolved,
            "derived_from": author_tag,
            "local_id": local_id,
        }
        # Carry TRIO fields forward when present
        for key in _TRIO_SLICE_KEYS:
            if key in pd and pd[key] not in (None, ""):
                slice_dict[key] = pd[key]
        plan = Plan(
            id=new_id("plan"),
            objective_id=objective_id,
            slice=slice_dict,
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


# ---------------------------------------------------------------------------
# TRIO variant
# ---------------------------------------------------------------------------


class PlanDraftTrioSkill(PlanDraftSkill):
    """plan_draft variant that requires TRIO structured output per plan.

    The flat `plan_draft` skill produces `{local_id, label, depends_on}`
    per plan — the label does double duty as both display text and as
    the contract for downstream consumers. TRIO forces the schema to
    expose those structural commitments as named fields.

    CRITICAL: this skill REQUIRES a SkillContext at construction time.
    Without a context it would produce target_impl paths against an
    imaginary repo layout (the hallucination failure mode Query #3
    documented and the 2025-04-13 sanity A/B confirmed). The context
    supplies the repo inventory (real files + symbols) that gets
    rendered into the prompt AND enforced by validate_output. A skill
    produced without context raises at construction time.

    TRIO fields per plan:
      - target_impl: "path/to/file.py::symbol_name". MUST reference a
        real file in the repo OR be paired with creates_new_file=true
        and match the project's impl-root convention.
      - target_test: "tests/path/test_file.py::test_name". Same rules
        as target_impl but against the test-root convention.
      - creates_new_file: optional bool, default false. Opt-in flag for
        plans that genuinely create a new file (vs modifying an
        existing one). Validated: if true, path must match convention;
        if false, path must exist in the repo inventory.
      - transformation: one-sentence imperative description. Required.
      - input_samples: non-empty list of concrete input examples.
      - output_samples: non-empty list, length matches input_samples.
      - resources: optional external deps list.

    Atomicity invariants (all enforced by validate_output):
      - Each target_impl path unique across plans.
      - Each target_test path unique across plans.
      - output_samples length == input_samples length per plan.
      - Every target_impl either exists in repo_inventory OR is marked
        creates_new_file=true AND matches an impl_root convention.
      - Same rule for target_test against test_root convention.
    """

    name = "plan_draft_trio"

    def __init__(self, context: "SkillContext | None" = None) -> None:
        if context is None:
            raise ValueError(
                "PlanDraftTrioSkill requires a SkillContext. Construct via "
                "`build_default_registry(skill_context=...)` or pass context "
                "explicitly. The skill cannot produce grounded output without "
                "the repo inventory."
            )
        self.context = context

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        base_prompt = super().build_prompt(inputs)
        # Render the repo inventory block with impl + test roots so the LLM
        # knows which files already exist.
        repo = self.context.repo
        inventory_block = repo.get_prompt_block(
            focus_prefixes=repo.impl_root_candidates + repo.test_root_candidates
        )
        trio_addendum = (
            "\n\nADDITIONAL REQUIREMENT — TRIO structured output:\n"
            "Each plan MUST also include the following fields:\n"
            "  - target_impl: the implementation target in the form\n"
            "    'path/to/file.py::symbol_name' (e.g.\n"
            "    'src/accruvia_harness/domain.py::Plan.summary').\n"
            "    Required unless the plan is test-only (then set this to null).\n"
            "    The path MUST be either (a) a file already in the repository\n"
            "    inventory below, or (b) a new file for which you also set\n"
            "    creates_new_file=true and whose path starts with an impl\n"
            f"    root from: {list(repo.impl_root_candidates)}.\n"
            "  - target_test: the test target in the form\n"
            "    'tests/path/test_file.py::test_name'. Required unless the plan\n"
            "    is impl-only. Same existence / creates_new_file rules apply,\n"
            f"    but against test roots: {list(repo.test_root_candidates)}.\n"
            "  - creates_new_file: bool, default false. Set to true ONLY when\n"
            "    the plan legitimately introduces a new file. Must pair with a\n"
            "    path under the impl-or-test root conventions above. If the\n"
            "    file already exists in the inventory, leave this false.\n"
            "  - transformation: one imperative sentence describing what the\n"
            "    plan's code does. Example: 'Return a one-line string showing\n"
            "    plan id, objective id, and status.'\n"
            "  - input_samples: list of at least one concrete input example.\n"
            "    Each element is a dict (or scalar) representing a real input\n"
            "    the function/test will see. Include edge cases where the\n"
            "    objective's non-negotiables would force them.\n"
            "  - output_samples: list of matching outputs, one per input_sample,\n"
            "    in the same order. Same shape as whatever the impl returns.\n"
            "  - resources: optional list of external deps (libraries, services,\n"
            "    env vars). Empty list if none.\n"
            "  - supersedes: list of existing 'path::symbol' entries that this\n"
            "    plan's transformation will replace, deprecate, or obviate.\n"
            "    Empty list [] if the plan adds new code without displacing\n"
            "    anything. Every entry MUST reference a real symbol in an\n"
            "    existing file. Use this to make the plan's cleanup impact\n"
            "    visible: if you refactor `OldClass.old_method` into a new\n"
            "    `NewService.run`, list `src/.../old_file.py::OldClass.old_method`\n"
            "    in supersedes.\n"
            "  - orphan_strategy: exactly one of 'absorb' | 'follow_up' | 'accept'\n"
            "    when supersedes is non-empty; null when supersedes is empty.\n"
            "    Meaning:\n"
            "      * absorb: this plan's transformation ALSO migrates every\n"
            "        caller of the superseded symbol. No cleanup plan needed.\n"
            "        Use when the call graph is small and cleanup fits in the\n"
            "        same atomic unit.\n"
            "      * follow_up: a companion plan in the same list handles\n"
            "        caller migration + dead-code removal. Use when the\n"
            "        migration scope would make this plan too large.\n"
            "      * accept: the superseded symbol is intentionally left in\n"
            "        place (public API, feature flag, save-for-later, dynamic\n"
            "        dispatch target). The orphan is NOT a bug. Requires\n"
            "        orphan_acceptance_reason below.\n"
            "  - orphan_acceptance_reason: one-sentence justification. Required\n"
            "    iff orphan_strategy == 'accept'.\n\n"
            "HARD INVARIANTS (violations cause rejection):\n"
            "  - Plans must have target_impl + target_test, or only target_impl,\n"
            "    or only target_test — at least one is required.\n"
            "  - Every target_impl path is unique across plans.\n"
            "  - Every target_test path is unique across plans.\n"
            "  - len(output_samples) == len(input_samples) per plan.\n"
            "  - Every target_impl / target_test path MUST either exist in the\n"
            "    repository inventory below OR be marked creates_new_file=true\n"
            "    AND match a valid impl/test root. Hallucinated paths are\n"
            "    REJECTED and you will be asked to try again.\n"
            "  - Every supersedes entry MUST reference a file AND symbol that\n"
            "    already exists in the repository.\n"
            "  - If supersedes is non-empty, orphan_strategy MUST be set.\n"
            "  - If orphan_strategy == 'accept', orphan_acceptance_reason\n"
            "    MUST be a non-empty string explaining why the orphan stays.\n\n"
            "Example plan entry with TRIO:\n"
            "{\n"
            '  "local_id": "p1",\n'
            '  "label": "Replace Plan.__repr__ with Plan.summary",\n'
            '  "depends_on": [],\n'
            '  "target_impl": "src/accruvia_harness/domain.py::Plan.summary",\n'
            '  "target_test": "tests/test_domain.py::test_plan_summary",\n'
            '  "creates_new_file": false,\n'
            '  "transformation": "Add Plan.summary() and migrate the 2 call sites in ui.py and bench.py from plan.__repr__() to plan.summary()",\n'
            '  "input_samples": [\n'
            '    {"id": "plan_abc123", "objective_id": "obj_def456", "status": "approved"}\n'
            "  ],\n"
            '  "output_samples": [\n'
            '    "plan_abc123 -> obj_def456 (approved)"\n'
            "  ],\n"
            '  "resources": [],\n'
            '  "supersedes": ["src/accruvia_harness/domain.py::Plan.__repr__"],\n'
            '  "orphan_strategy": "absorb",\n'
            '  "orphan_acceptance_reason": null\n'
            "}\n\n"
            f"{inventory_block}\n"
        )
        return base_prompt + trio_addendum

    def parse_response(self, response_text: str) -> dict[str, Any]:
        base_parsed = super().parse_response(response_text)
        raw = extract_json_payload(response_text) or {}
        raw_plans = raw.get("plans") if isinstance(raw, dict) else None
        if not isinstance(raw_plans, list):
            return base_parsed

        # Enrich each already-parsed plan with TRIO fields from the raw payload.
        # Match by local_id to be resilient to dropped malformed entries.
        raw_by_id = {}
        for item in raw_plans:
            if isinstance(item, dict):
                lid = str(item.get("local_id") or "").strip()
                if lid:
                    raw_by_id[lid] = item

        for plan in base_parsed.get("plans") or []:
            src = raw_by_id.get(plan["local_id"]) or {}
            for key in ("target_impl", "target_test", "transformation"):
                val = src.get(key)
                if isinstance(val, str) and val.strip():
                    plan[key] = val.strip()
            for key in ("input_samples", "output_samples"):
                val = src.get(key)
                if isinstance(val, list):
                    plan[key] = val
            resources = src.get("resources")
            if isinstance(resources, list):
                plan["resources"] = [str(r).strip() for r in resources if str(r).strip()]
            # supersedes: list of 'path::symbol' entries
            supersedes = src.get("supersedes")
            if isinstance(supersedes, list):
                plan["supersedes"] = [
                    str(s).strip() for s in supersedes if isinstance(s, str) and s.strip()
                ]
            else:
                plan["supersedes"] = []
            # orphan_strategy: absorb | follow_up | accept | null
            strategy = src.get("orphan_strategy")
            if isinstance(strategy, str) and strategy.strip():
                normalized = strategy.strip().lower().replace("-", "_")
                if normalized in ("absorb", "follow_up", "accept"):
                    plan["orphan_strategy"] = normalized
                else:
                    plan["orphan_strategy"] = None
            else:
                plan["orphan_strategy"] = None
            # orphan_acceptance_reason: non-empty string when strategy=accept
            reason = src.get("orphan_acceptance_reason")
            plan["orphan_acceptance_reason"] = (
                str(reason).strip() if isinstance(reason, str) else ""
            )
            # creates_new_file starts as whatever the LLM declared.
            declared = bool(src.get("creates_new_file") or False)
            plan["creates_new_file"] = declared

            # AUTO-INFERENCE (option 2): the LLM frequently forgets the
            # creates_new_file flag when its intent is genuinely to create
            # a new file for a refactor. If the declared flag is False but
            # at least one of the target paths points at a file that does
            # not exist in the repo AND the path matches an impl-or-test
            # root convention, upgrade creates_new_file to True. This
            # distinguishes two failure modes:
            #   (a) path missing + matches convention → likely "forgot the
            #       flag" → auto-correct
            #   (b) path missing + violates convention → genuine
            #       hallucination → validator will reject as before
            if not declared:
                repo = self.context.repo
                needs_new = False
                for tgt_key, is_impl in (("target_impl", True), ("target_test", False)):
                    val = plan.get(tgt_key)
                    if not isinstance(val, str) or not val.strip():
                        continue
                    path = val.split("::", 1)[0].strip()
                    if repo.file_exists(path):
                        continue
                    # Path missing — does it match a convention?
                    if is_impl and repo.path_matches_impl_convention(path):
                        needs_new = True
                        break
                    if (not is_impl) and repo.path_matches_test_convention(path):
                        needs_new = True
                        break
                if needs_new:
                    plan["creates_new_file"] = True
                    plan.setdefault("_creates_new_file_auto_inferred", True)
        return base_parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = super().validate_output(parsed)
        if not ok:
            return False, errors

        repo = self.context.repo
        plans = parsed.get("plans") or []
        trio_errors: list[str] = []
        seen_impl: dict[str, str] = {}
        seen_test: dict[str, str] = {}

        for plan in plans:
            local_id = plan["local_id"]
            target_impl = plan.get("target_impl")
            target_test = plan.get("target_test")
            creates_new_file = bool(plan.get("creates_new_file") or False)

            if not target_impl and not target_test:
                trio_errors.append(
                    f"plan {local_id!r} has neither target_impl nor target_test — "
                    "at least one is required"
                )
                continue

            if target_impl:
                if target_impl in seen_impl:
                    trio_errors.append(
                        f"plan {local_id!r} target_impl {target_impl!r} already claimed "
                        f"by plan {seen_impl[target_impl]!r}"
                    )
                else:
                    seen_impl[target_impl] = local_id
                # Path-existence check. Split `path::symbol` first.
                impl_path = target_impl.split("::", 1)[0]
                if repo.file_exists(impl_path):
                    # File exists. Nothing more to check at the path level.
                    pass
                elif creates_new_file:
                    if not repo.path_matches_impl_convention(impl_path):
                        trio_errors.append(
                            f"plan {local_id!r} target_impl {impl_path!r} marked "
                            f"creates_new_file=true but does not match an impl-root "
                            f"convention {list(repo.impl_root_candidates)}"
                        )
                else:
                    trio_errors.append(
                        f"plan {local_id!r} target_impl path {impl_path!r} is not in the "
                        f"repository inventory and creates_new_file is not set. "
                        f"Hallucinated path — either reference a real file or set "
                        f"creates_new_file=true."
                    )

            if target_test:
                if target_test in seen_test:
                    trio_errors.append(
                        f"plan {local_id!r} target_test {target_test!r} already claimed "
                        f"by plan {seen_test[target_test]!r}"
                    )
                else:
                    seen_test[target_test] = local_id
                test_path = target_test.split("::", 1)[0]
                if repo.file_exists(test_path):
                    pass
                elif creates_new_file:
                    if not repo.path_matches_test_convention(test_path):
                        trio_errors.append(
                            f"plan {local_id!r} target_test {test_path!r} marked "
                            f"creates_new_file=true but does not match a test-root "
                            f"convention {list(repo.test_root_candidates)}"
                        )
                else:
                    trio_errors.append(
                        f"plan {local_id!r} target_test path {test_path!r} is not in the "
                        f"repository inventory and creates_new_file is not set. "
                        f"Hallucinated path — either reference a real test file or set "
                        f"creates_new_file=true."
                    )

            # Orphan invariants — primary line of defense, not red-team loop.
            supersedes = plan.get("supersedes") or []
            if not isinstance(supersedes, list):
                trio_errors.append(
                    f"plan {local_id!r} supersedes must be a list of 'path::symbol' entries"
                )
                supersedes = []
            strategy = plan.get("orphan_strategy")
            if supersedes:
                # Each superseded entry must reference a real file + real symbol
                for sup in supersedes:
                    if "::" not in sup:
                        trio_errors.append(
                            f"plan {local_id!r} supersedes entry {sup!r} must be in 'path::symbol' form"
                        )
                        continue
                    sup_path, sup_symbol = sup.split("::", 1)
                    sup_path = sup_path.strip()
                    sup_symbol = sup_symbol.strip()
                    if not repo.file_exists(sup_path):
                        trio_errors.append(
                            f"plan {local_id!r} supersedes {sup!r}: file does not exist in inventory"
                        )
                        continue
                    # Symbol existence check only applies to Python files we can AST-parse
                    if sup_path.endswith(".py") and not repo.symbol_exists(sup_path, sup_symbol):
                        trio_errors.append(
                            f"plan {local_id!r} supersedes {sup!r}: symbol {sup_symbol!r} not "
                            f"defined at top level of {sup_path!r}"
                        )
                # orphan_strategy must be set
                if strategy not in ("absorb", "follow_up", "accept"):
                    trio_errors.append(
                        f"plan {local_id!r} has supersedes={supersedes!r} but "
                        f"orphan_strategy is not one of absorb/follow_up/accept"
                    )
                # orphan_acceptance_reason required when strategy=accept
                if strategy == "accept":
                    reason = (plan.get("orphan_acceptance_reason") or "").strip()
                    if not reason:
                        trio_errors.append(
                            f"plan {local_id!r} orphan_strategy=accept but "
                            f"orphan_acceptance_reason is empty — explain why "
                            f"the orphan is intentional"
                        )
            else:
                # No supersedes → strategy must be null
                if strategy is not None:
                    trio_errors.append(
                        f"plan {local_id!r} has no supersedes but declares "
                        f"orphan_strategy={strategy!r} — set supersedes to a "
                        f"non-empty list or set orphan_strategy to null"
                    )

            transformation = plan.get("transformation")
            if not transformation or not str(transformation).strip():
                trio_errors.append(f"plan {local_id!r} missing transformation")

            input_samples = plan.get("input_samples")
            output_samples = plan.get("output_samples")
            if not isinstance(input_samples, list) or not input_samples:
                trio_errors.append(
                    f"plan {local_id!r} missing input_samples (must be non-empty list)"
                )
            if not isinstance(output_samples, list) or not output_samples:
                trio_errors.append(
                    f"plan {local_id!r} missing output_samples (must be non-empty list)"
                )
            if (
                isinstance(input_samples, list)
                and isinstance(output_samples, list)
                and len(input_samples) != len(output_samples)
            ):
                trio_errors.append(
                    f"plan {local_id!r} input_samples ({len(input_samples)}) and "
                    f"output_samples ({len(output_samples)}) length mismatch"
                )

        return (not trio_errors, trio_errors)
