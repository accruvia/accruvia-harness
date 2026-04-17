"""Canonical Mermaid render + canonicalize post-processor.

See module docstring in `mermaid/__init__.py` for the invariant this enforces.

The two main entry points are:

    render_mermaid_from_plans(plans, objective) -> str
        Deterministic render. Given a list of plans, produce a flowchart
        with stable `P_<hash>` IDs. Pure function — no LLM, no I/O.

    canonicalize_mermaid(proposed_text, authoritative_plans) -> CanonicalizeResult
        Validate an LLM-proposed (or operator-edited) Mermaid text against
        the authoritative plan set. Enforces the 7 rejection rules and
        emits a normalized canonical text when accepted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..domain import Objective, Plan


# ---------------------------------------------------------------------------
# Canonical ID generator
# ---------------------------------------------------------------------------


_ID_PREFIX = "P_"
_ID_SUFFIX_LEN = 12


def canonical_node_id(plan: Plan) -> str:
    """Return the stable mermaid node id for a plan.

    Shape: `P_<last 12 chars of the plan id's non-prefix portion>`. The
    `P_` prefix distinguishes plan-anchored nodes from flowchart aliases
    like `A`, `B`, `O`, making the invariant visible in rendered diagrams.
    """
    if "_" in plan.id:
        suffix = plan.id.split("_", 1)[1]
    else:
        suffix = plan.id
    return f"{_ID_PREFIX}{suffix[:_ID_SUFFIX_LEN]}"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


_OBJECTIVE_NODE_ID = "O"


def _mermaid_escape_label(text: str) -> str:
    """Escape a label for safe inclusion in a Mermaid node declaration.

    Mermaid node labels in `id["..."]` form treat double quotes as the
    delimiter; internal double quotes must be replaced. Newlines become
    `<br/>` which Mermaid renders as a soft break inside the node.
    """
    return text.replace('"', "'").replace("\n", "<br/>").strip()


def _plan_label(plan: Plan) -> str:
    """Derive a human-readable label for a plan node.

    Priority: `slice["label"]` explicit > `slice["title"]` > `slice["task_title"]`
    > fallback to the plan id. Falls back in a documented order so renders
    stay stable as the slice schema evolves.
    """
    from ..domain import plan_slice_typed
    sl = plan_slice_typed(plan)
    if sl.label:
        return sl.label
    slice_dict = plan.slice or {}
    for key in ("title", "task_title"):
        candidate = slice_dict.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return plan.id


def _plan_dependencies(plan: Plan) -> list[str]:
    """Return the list of plan ids this plan depends on.

    Dependencies live in `slice["dependencies"]` as a list of plan ids.
    Unknown or missing dependencies produce an empty list rather than
    raising — the renderer is forgiving because dependency data is user
    content, not a system invariant.
    """
    from ..domain import plan_slice_typed
    sl = plan_slice_typed(plan)
    return [str(d).strip() for d in sl.dependencies if isinstance(d, str) and d.strip()]


def render_mermaid_from_plans(
    plans: Iterable[Plan],
    objective: Objective,
) -> str:
    """Deterministically render a flowchart TD diagram from a plan set.

    The output is byte-identical for the same (plans, objective) input.
    Node IDs are `canonical_node_id(plan)`; the objective is always
    rendered as the `O` node with an edge to every root plan (one with
    no dependencies inside the plan set). Dependencies are rendered as
    edges between plan nodes.

    If `plans` is empty, emits a placeholder `O --> AWAITING` diagram
    that downstream code can detect as "not yet decomposed".
    """
    plan_list = [p for p in plans if p.approval_status != "rejected"]
    lines: list[str] = ["flowchart TD"]
    objective_label = _mermaid_escape_label(f"Objective: {objective.title}")
    lines.append(f'    {_OBJECTIVE_NODE_ID}["{objective_label}"]')

    if not plan_list:
        lines.append('    O --> AWAITING["awaiting decomposition"]')
        return "\n".join(lines)

    # Sort plans for deterministic output: by created_at, then id.
    plan_list = sorted(plan_list, key=lambda p: (p.created_at, p.id))

    # Index plans by canonical node id for dependency resolution.
    plan_by_canonical: dict[str, Plan] = {}
    for p in plan_list:
        plan_by_canonical[canonical_node_id(p)] = p
    # Also index by plan.id so dependencies can be stored as either form.
    plan_by_id: dict[str, Plan] = {p.id: p for p in plan_list}

    # Emit node declarations.
    for plan in plan_list:
        node_id = canonical_node_id(plan)
        label = _mermaid_escape_label(_plan_label(plan))
        lines.append(f'    {node_id}["{label}"]')

    # Resolve dependency references to canonical node ids.
    edges: list[tuple[str, str]] = []
    dependents: set[str] = set()
    for plan in plan_list:
        target = canonical_node_id(plan)
        for dep_ref in _plan_dependencies(plan):
            # Accept either a canonical P_ id or a raw plan.id
            if dep_ref.startswith(_ID_PREFIX) and dep_ref in plan_by_canonical:
                source = dep_ref
            elif dep_ref in plan_by_id:
                source = canonical_node_id(plan_by_id[dep_ref])
            else:
                # Unknown dependency — skip rather than break the render.
                continue
            edges.append((source, target))
            dependents.add(target)

    # Any plan with no inbound edge is a root; connect it to O.
    for plan in plan_list:
        node_id = canonical_node_id(plan)
        if node_id not in dependents:
            lines.append(f"    {_OBJECTIVE_NODE_ID} --> {node_id}")

    # Emit the resolved dependency edges in deterministic order.
    for source, target in sorted(edges):
        lines.append(f"    {source} --> {target}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Canonicalize
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PlanOp:
    """An implied change to the plan set derived from a proposed Mermaid."""

    kind: str  # "add" | "remove" | "rename"
    plan_id: str | None  # None for "add"
    proposed_label: str = ""
    proposed_dependencies: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CanonicalizeResult:
    accepted: bool
    canonical_text: str | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    plan_operations: list[PlanOp] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)  # proposed_id -> plan_id
    unmapped_proposed: list[str] = field(default_factory=list)
    unmapped_plans: list[str] = field(default_factory=list)


_NODE_DECL_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:\[\"([^\"]*)\"\]|\[([^\]]*)\]|\(([^)]*)\)|\{([^}]*)\})"
)
_HEADER_RE = re.compile(r"^\s*(flowchart|graph)\s+(TD|TB|BT|LR|RL)\b", re.IGNORECASE)
_NONFLOW_RE = re.compile(
    r"^\s*(stateDiagram|sequenceDiagram|classDiagram|erDiagram|gantt|pie|journey)",
    re.IGNORECASE,
)
_EDGE_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:--+>|==+>|-\.->?|~~~>?)\s*(?:\|[^|]*\|\s*)?([A-Za-z_][A-Za-z0-9_]*)"
)


def _parse_mermaid(text: str) -> tuple[str, dict[str, str], list[tuple[str, str]], list[str]]:
    """Return (header_direction, declared_nodes, edges, errors).

    declared_nodes maps `id -> label` for nodes that appeared in an explicit
    `id[label]` (or `(label)`, `{label}`) declaration. Nodes that only show
    up as edge endpoints are NOT added — that's the signal for "broken
    edge" which canonicalize_mermaid treats as a rejection. The objective
    root `O` is whitelisted separately because it's structural, not a plan.
    """
    errors: list[str] = []
    direction = "TD"
    nodes: dict[str, str] = {}
    edges: list[tuple[str, str]] = []

    # Strip optional code fence.
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]

    saw_header = False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("%%"):
            continue
        if _NONFLOW_RE.match(line):
            errors.append(
                f"non-flowchart diagram type: {line.split()[0]}; only flowchart is supported"
            )
            return direction, nodes, edges, errors
        m = _HEADER_RE.match(line)
        if m:
            saw_header = True
            direction = m.group(2).upper()
            continue
        if line.startswith("subgraph") or line == "end":
            continue
        if line.startswith(("classDef", "class ", "style ", "linkStyle", "click ")):
            continue
        # Node declaration?
        nm = _NODE_DECL_RE.match(line)
        if nm:
            node_id = nm.group(1)
            label = next((g for g in nm.groups()[1:] if g is not None), "")
            if node_id in nodes:
                errors.append(f"duplicate node declaration: {node_id}")
            else:
                nodes[node_id] = label.strip()
        # Edges can share a line with or without a declaration.
        # IMPORTANT: do NOT auto-declare edge participants; canonicalize
        # checks that edge endpoints were explicitly declared, which is
        # what catches "broken edge" in the rejection rules.
        for em in _EDGE_RE.finditer(line):
            src, dst = em.group(1), em.group(2)
            if src and dst:
                edges.append((src, dst))

    if not saw_header:
        errors.append("missing flowchart header")
    return direction, nodes, edges, errors


def _ngrams(text: str, n: int = 3) -> set[str]:
    norm = re.sub(r"\s+", " ", text.lower().strip())
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i : i + n] for i in range(len(norm) - n + 1)}


def label_similarity(a: str, b: str) -> float:
    """Jaccard similarity on 3-grams of normalized labels.

    Returns a value in [0, 1]. 1.0 is an exact match after whitespace
    normalization; 0.0 has no 3-gram overlap. This metric is cheap,
    deterministic, and robust to word-order and short edits.
    """
    ga, gb = _ngrams(a), _ngrams(b)
    if not ga and not gb:
        return 1.0
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


_DEFAULT_SIMILARITY_THRESHOLD = 0.5


def canonicalize_mermaid(
    proposed_text: str,
    authoritative_plans: list[Plan],
    *,
    objective: Objective | None = None,
    allow_plan_adds: bool = False,
    label_similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
) -> CanonicalizeResult:
    """Validate a proposed Mermaid text against the authoritative plan set.

    Rejection rules (all must pass for `accepted=True`):
      1. Non-flowchart diagram type.
      2. Missing or malformed flowchart header.
      3. Duplicate node declarations.
      4. Broken edges (edge references an undeclared node).
      5. Dropped plan — an authoritative plan has no matching proposed node.
      6. Orphan proposed node with no matching plan (unless `allow_plan_adds=True`).
      7. Ambiguous mapping (two proposed nodes map to the same plan, or
         one proposed node has similarity >= threshold to multiple plans).

    On success, emits a normalized canonical text where every node id is
    the plan's canonical id and edges are rewritten to use canonical ids.
    """
    direction, proposed_nodes, proposed_edges, parse_errors = _parse_mermaid(proposed_text)
    result = CanonicalizeResult(accepted=False)
    result.rejection_reasons.extend(parse_errors)

    # Rule 4: broken edges
    for src, dst in proposed_edges:
        if src not in proposed_nodes:
            result.rejection_reasons.append(f"edge references undeclared node: {src}")
        if dst not in proposed_nodes:
            result.rejection_reasons.append(f"edge references undeclared node: {dst}")

    if result.rejection_reasons:
        return result

    # Drop the `O` objective node from mapping (it's always the objective root).
    proposed_nodes_to_match = {
        nid: lbl for nid, lbl in proposed_nodes.items() if nid != _OBJECTIVE_NODE_ID
    }
    # Drop the AWAITING placeholder if present.
    proposed_nodes_to_match = {
        nid: lbl for nid, lbl in proposed_nodes_to_match.items() if lbl != "awaiting decomposition"
    }

    plans_by_id = {p.id: p for p in authoritative_plans}
    plans_by_canonical = {canonical_node_id(p): p for p in authoritative_plans}

    # Phase 1: explicit canonical-id match.
    mapping: dict[str, str] = {}
    bound_plans: set[str] = set()
    for pid, _label in proposed_nodes_to_match.items():
        if pid in plans_by_canonical:
            plan = plans_by_canonical[pid]
            mapping[pid] = plan.id
            bound_plans.add(plan.id)

    # Phase 2: label-based match for remaining.
    remaining_proposed = [
        pid for pid in proposed_nodes_to_match if pid not in mapping
    ]
    remaining_plans = [
        p for p in authoritative_plans if p.id not in bound_plans
    ]

    # Compute similarity for all pairs.
    pairs: list[tuple[float, str, str]] = []
    for pid in remaining_proposed:
        plabel = proposed_nodes_to_match[pid]
        for plan in remaining_plans:
            sim = label_similarity(plabel, _plan_label(plan))
            if sim >= label_similarity_threshold:
                pairs.append((sim, pid, plan.id))

    # Greedy assignment by highest similarity first.
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    bound_proposed: set[str] = set()
    for sim, pid, plan_id in pairs:
        if pid in bound_proposed or plan_id in bound_plans:
            continue
        mapping[pid] = plan_id
        bound_proposed.add(pid)
        bound_plans.add(plan_id)

    # Phase 3: check for ambiguity — each proposed node that matched should
    # be the unique winner; verify no unmatched proposed node still has a
    # >= threshold similarity to two or more unbound plans.
    for pid in remaining_proposed:
        if pid in bound_proposed:
            continue
        plabel = proposed_nodes_to_match[pid]
        qualifying = [
            plan.id
            for plan in authoritative_plans
            if label_similarity(plabel, _plan_label(plan)) >= label_similarity_threshold
        ]
        if len(qualifying) > 1:
            result.rejection_reasons.append(
                f"ambiguous mapping: proposed node {pid!r} (label={plabel!r}) matches "
                f"multiple plans: {qualifying}"
            )

    result.mapping = mapping
    result.unmapped_proposed = [
        pid for pid in proposed_nodes_to_match if pid not in mapping
    ]
    result.unmapped_plans = [
        p.id for p in authoritative_plans if p.id not in bound_plans
    ]

    # Rule 5: dropped plans
    if result.unmapped_plans:
        for plan_id in result.unmapped_plans:
            plan = plans_by_id[plan_id]
            result.rejection_reasons.append(
                f"dropped plan: {plan_id} ({_plan_label(plan)!r}) has no node in the proposal"
            )

    # Rule 6: orphan proposed nodes
    if result.unmapped_proposed and not allow_plan_adds:
        for pid in result.unmapped_proposed:
            label = proposed_nodes_to_match[pid]
            result.rejection_reasons.append(
                f"orphan proposed node: {pid!r} (label={label!r}) has no matching plan"
            )

    if result.rejection_reasons:
        return result

    # If operator-initiated and new nodes are allowed, emit plan_add operations.
    if allow_plan_adds and result.unmapped_proposed:
        for pid in result.unmapped_proposed:
            result.plan_operations.append(
                PlanOp(
                    kind="add",
                    plan_id=None,
                    proposed_label=proposed_nodes_to_match[pid],
                )
            )
        # Plan adds require explicit approval — return accepted=False with
        # plan_operations populated so the caller can surface them.
        return result

    # Build canonical text.
    result.accepted = True
    if objective is not None:
        result.canonical_text = _emit_canonical(
            authoritative_plans=authoritative_plans,
            objective=objective,
            proposed_nodes=proposed_nodes_to_match,
            proposed_edges=proposed_edges,
            mapping=mapping,
            direction=direction,
        )
    else:
        # Without an objective, render a canonical-but-objective-less subset.
        # Used in tests; callers with a real objective should pass it.
        result.canonical_text = _emit_canonical_bare(
            authoritative_plans=authoritative_plans,
            proposed_edges=proposed_edges,
            mapping=mapping,
            direction=direction,
        )
    return result


def _emit_canonical(
    *,
    authoritative_plans: list[Plan],
    objective: Objective,
    proposed_nodes: dict[str, str],
    proposed_edges: list[tuple[str, str]],
    mapping: dict[str, str],
    direction: str,
) -> str:
    """Emit a normalized canonical Mermaid text.

    Uses the authoritative plans' labels (not the proposed ones — label
    changes must go through a separate plan-rename operation). Uses the
    LLM's proposed edges (rewritten to canonical ids), since edge topology
    is the legitimate area for LLM refinement.
    """
    lines: list[str] = [f"flowchart {direction}"]
    obj_label = _mermaid_escape_label(f"Objective: {objective.title}")
    lines.append(f'    {_OBJECTIVE_NODE_ID}["{obj_label}"]')

    plan_by_id = {p.id: p for p in authoritative_plans}
    sorted_plans = sorted(authoritative_plans, key=lambda p: (p.created_at, p.id))
    for plan in sorted_plans:
        nid = canonical_node_id(plan)
        label = _mermaid_escape_label(_plan_label(plan))
        lines.append(f'    {nid}["{label}"]')

    # Translate edges to canonical ids.
    reverse_map = {pid: plan_id for pid, plan_id in mapping.items()}
    canonical_edges: list[tuple[str, str]] = []
    dependents: set[str] = set()
    for src, dst in proposed_edges:
        canonical_src: str | None = None
        canonical_dst: str | None = None
        if src == _OBJECTIVE_NODE_ID:
            canonical_src = _OBJECTIVE_NODE_ID
        elif src in reverse_map:
            canonical_src = canonical_node_id(plan_by_id[reverse_map[src]])
        if dst == _OBJECTIVE_NODE_ID:
            canonical_dst = _OBJECTIVE_NODE_ID
        elif dst in reverse_map:
            canonical_dst = canonical_node_id(plan_by_id[reverse_map[dst]])
        if canonical_src and canonical_dst:
            canonical_edges.append((canonical_src, canonical_dst))
            if canonical_src != _OBJECTIVE_NODE_ID:
                dependents.add(canonical_dst)

    # Ensure every plan has a path from O: add O -> plan for roots.
    for plan in sorted_plans:
        nid = canonical_node_id(plan)
        if nid not in dependents and (_OBJECTIVE_NODE_ID, nid) not in canonical_edges:
            canonical_edges.append((_OBJECTIVE_NODE_ID, nid))

    for src, dst in sorted(set(canonical_edges)):
        lines.append(f"    {src} --> {dst}")
    return "\n".join(lines)


def _emit_canonical_bare(
    *,
    authoritative_plans: list[Plan],
    proposed_edges: list[tuple[str, str]],
    mapping: dict[str, str],
    direction: str,
) -> str:
    lines: list[str] = [f"flowchart {direction}"]
    plan_by_id = {p.id: p for p in authoritative_plans}
    sorted_plans = sorted(authoritative_plans, key=lambda p: (p.created_at, p.id))
    for plan in sorted_plans:
        nid = canonical_node_id(plan)
        label = _mermaid_escape_label(_plan_label(plan))
        lines.append(f'    {nid}["{label}"]')
    for src, dst in proposed_edges:
        if src in mapping and dst in mapping:
            canonical_src = canonical_node_id(plan_by_id[mapping[src]])
            canonical_dst = canonical_node_id(plan_by_id[mapping[dst]])
            lines.append(f"    {canonical_src} --> {canonical_dst}")
    return "\n".join(lines)
