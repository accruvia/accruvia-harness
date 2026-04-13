"""Canonical Mermaid rendering and ID-stability enforcement.

This module is the single source of truth for the mapping between Plan rows
and Mermaid node IDs. Every consumer of `tasks.mermaid_node_id`,
`plans.mermaid_node_id`, and Mermaid artifact content must go through the
functions exported here.

Core invariant: for every Plan row, `plan.mermaid_node_id == canonical_node_id(plan)`.
The ID is assigned once at plan creation and never changes. Mermaid text is
rendered deterministically from plans; LLM-proposed Mermaid updates are
validated against the authoritative plan set before being persisted.

See the design notes in the red-team loop / Query #3 findings for why this
exists: historically, `mermaid_node_id` was synthesized from task IDs by
`_ensure_plan_linkage`, and LLM mermaid rewrites used flowchart-idiomatic
single-letter IDs that didn't survive regeneration. Both paths are removed
in favor of the single canonical scheme defined here.
"""
from .render import (
    CanonicalizeResult,
    PlanOp,
    canonical_node_id,
    canonicalize_mermaid,
    label_similarity,
    render_mermaid_from_plans,
)

__all__ = [
    "CanonicalizeResult",
    "PlanOp",
    "canonical_node_id",
    "canonicalize_mermaid",
    "label_similarity",
    "render_mermaid_from_plans",
]
