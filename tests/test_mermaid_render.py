"""Tests for the canonical Mermaid render + canonicalize layer."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from accruvia_harness.domain import Objective, ObjectiveStatus, Plan
from accruvia_harness.mermaid import (
    CanonicalizeResult,
    PlanOp,
    canonical_node_id,
    canonicalize_mermaid,
    label_similarity,
    render_mermaid_from_plans,
)


def _plan(id_suffix: str, label: str, *, deps: list[str] | None = None, created_offset: int = 0) -> Plan:
    slice_dict = {"label": label}
    if deps:
        slice_dict["dependencies"] = deps
    return Plan(
        id=f"plan_{id_suffix}",
        objective_id="objective_test",
        slice=slice_dict,
        created_at=datetime(2026, 4, 13, 10, 0, created_offset, tzinfo=timezone.utc),
        approval_status="approved",
    )


def _objective(title: str = "Test objective") -> Objective:
    return Objective(
        id="objective_test",
        project_id="proj_test",
        title=title,
        summary="test",
        status=ObjectiveStatus.OPEN,
        created_at=datetime(2026, 4, 13, 9, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 13, 9, 0, 0, tzinfo=timezone.utc),
    )


class CanonicalNodeIdTests(unittest.TestCase):
    def test_id_derives_from_plan_id_suffix(self):
        plan = _plan("abc123def456extra", "x")
        self.assertEqual("P_abc123def456", canonical_node_id(plan))

    def test_id_is_stable_across_calls(self):
        plan = _plan("abc123", "x")
        self.assertEqual(canonical_node_id(plan), canonical_node_id(plan))

    def test_id_handles_id_without_prefix(self):
        plan = Plan(id="no_underscore_value", objective_id="o", slice={"label": "x"})
        self.assertEqual("P_underscore_v", canonical_node_id(plan))


class RenderFromPlansTests(unittest.TestCase):
    def test_empty_plan_list_emits_awaiting_placeholder(self):
        obj = _objective("Add feature X")
        text = render_mermaid_from_plans([], obj)
        self.assertIn('O["Objective: Add feature X"]', text)
        self.assertIn("AWAITING", text)
        self.assertIn("awaiting decomposition", text)

    def test_single_plan_rooted_at_objective(self):
        obj = _objective()
        plans = [_plan("abc123", "Add Plan.summary method")]
        text = render_mermaid_from_plans(plans, obj)
        self.assertIn("flowchart TD", text)
        self.assertIn('P_abc123["Add Plan.summary method"]', text)
        self.assertIn("O --> P_abc123", text)

    def test_two_plans_with_dependency_edge(self):
        obj = _objective()
        p1 = _plan("abc123", "Add method", created_offset=1)
        p2 = _plan("def456", "Add test", deps=["plan_abc123"], created_offset=2)
        text = render_mermaid_from_plans([p1, p2], obj)
        self.assertIn('P_abc123["Add method"]', text)
        self.assertIn('P_def456["Add test"]', text)
        # p1 is a root: O -> P_abc123
        self.assertIn("O --> P_abc123", text)
        # p2 depends on p1: P_abc123 -> P_def456
        self.assertIn("P_abc123 --> P_def456", text)
        # p2 should NOT be directly rooted at O (it has an inbound edge)
        self.assertNotIn("O --> P_def456", text)

    def test_render_is_deterministic(self):
        obj = _objective()
        plans = [_plan("abc123", "Add method"), _plan("def456", "Add test", created_offset=1)]
        text_a = render_mermaid_from_plans(plans, obj)
        text_b = render_mermaid_from_plans(plans, obj)
        self.assertEqual(text_a, text_b)

    def test_rejected_plan_is_not_rendered(self):
        obj = _objective()
        p1 = _plan("abc123", "Add method")
        p2 = _plan("def456", "Rejected idea")
        p2.approval_status = "rejected"
        text = render_mermaid_from_plans([p1, p2], obj)
        self.assertIn("P_abc123", text)
        self.assertNotIn("P_def456", text)

    def test_label_escaping_strips_double_quotes_and_newlines(self):
        obj = _objective('Title with "quotes"')
        plans = [_plan("abc123", 'Label with "quotes"\nand newline')]
        text = render_mermaid_from_plans(plans, obj)
        self.assertNotIn('"quotes"]', text)  # internal quotes escaped
        self.assertIn("Label with 'quotes'<br/>and newline", text)


class LabelSimilarityTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(1.0, label_similarity("add summary method", "add summary method"))

    def test_case_insensitive(self):
        self.assertEqual(1.0, label_similarity("Add Summary", "add summary"))

    def test_unrelated_strings(self):
        self.assertLess(label_similarity("add summary", "delete temporal"), 0.1)

    def test_minor_edit_scores_high(self):
        # "Add summary() method" vs "Add summary() methods" (typo/plural)
        self.assertGreater(
            label_similarity("add summary() method", "add summary() methods"),
            0.8,
        )

    def test_word_order_robust(self):
        # Jaccard on 3-grams handles word-order shuffles reasonably.
        sim = label_similarity("add summary to plan", "add plan summary")
        self.assertGreater(sim, 0.4)


class CanonicalizeMermaidTests(unittest.TestCase):
    def _make_plans(self) -> list[Plan]:
        return [
            _plan("abc123def456", "Add summary method to Plan", created_offset=1),
            _plan("ghi789jkl012", "Add unit test for summary", created_offset=2),
        ]

    def test_accepts_proposal_using_canonical_ids(self):
        plans = self._make_plans()
        proposed = """
flowchart TD
    O["Objective: test"]
    P_abc123def456["Add summary method to Plan"]
    P_ghi789jkl012["Add unit test for summary"]
    O --> P_abc123def456
    P_abc123def456 --> P_ghi789jkl012
"""
        result = canonicalize_mermaid(proposed, plans, objective=_objective())
        self.assertTrue(result.accepted, msg=result.rejection_reasons)
        self.assertEqual(2, len(result.mapping))
        self.assertIn("P_abc123def456 --> P_ghi789jkl012", result.canonical_text)

    def test_accepts_proposal_with_flowchart_aliases_via_label_match(self):
        plans = self._make_plans()
        # LLM uses A/B aliases; the post-processor should match by label similarity.
        proposed = """
flowchart TD
    O["Objective: test"]
    A["Add summary method to Plan"]
    B["Add unit test for summary"]
    O --> A
    A --> B
"""
        result = canonicalize_mermaid(proposed, plans, objective=_objective())
        self.assertTrue(result.accepted, msg=result.rejection_reasons)
        # A should bind to plan_abc123def456, B to plan_ghi789jkl012
        self.assertEqual("plan_abc123def456", result.mapping["A"])
        self.assertEqual("plan_ghi789jkl012", result.mapping["B"])
        # Canonical text should use P_<hash> ids.
        self.assertIn("P_abc123def456", result.canonical_text)
        self.assertIn("P_ghi789jkl012", result.canonical_text)
        self.assertNotIn(' A["', result.canonical_text)

    def test_rejects_non_flowchart_diagram(self):
        plans = self._make_plans()
        proposed = "stateDiagram-v2\n    [*] --> Still"
        result = canonicalize_mermaid(proposed, plans, objective=_objective())
        self.assertFalse(result.accepted)
        self.assertTrue(
            any("non-flowchart" in r for r in result.rejection_reasons),
            result.rejection_reasons,
        )

    def test_rejects_missing_header(self):
        plans = self._make_plans()
        proposed = '    A["Add summary method to Plan"]\n    B["Add unit test for summary"]'
        result = canonicalize_mermaid(proposed, plans, objective=_objective())
        self.assertFalse(result.accepted)
        self.assertTrue(
            any("missing flowchart header" in r for r in result.rejection_reasons),
            result.rejection_reasons,
        )

    def test_rejects_dropped_plan(self):
        plans = self._make_plans()
        # Proposal contains only the first plan; the second is dropped.
        proposed = """
flowchart TD
    O["Objective: test"]
    A["Add summary method to Plan"]
    O --> A
"""
        result = canonicalize_mermaid(proposed, plans, objective=_objective())
        self.assertFalse(result.accepted)
        self.assertTrue(
            any("dropped plan" in r for r in result.rejection_reasons),
            result.rejection_reasons,
        )

    def test_rejects_orphan_proposed_node_when_adds_not_allowed(self):
        plans = self._make_plans()
        proposed = """
flowchart TD
    O["Objective: test"]
    A["Add summary method to Plan"]
    B["Add unit test for summary"]
    C["Add completely unrelated new feature"]
    O --> A
    A --> B
    B --> C
"""
        result = canonicalize_mermaid(proposed, plans, objective=_objective())
        self.assertFalse(result.accepted)
        self.assertTrue(
            any("orphan proposed node" in r for r in result.rejection_reasons),
            result.rejection_reasons,
        )

    def test_allow_plan_adds_yields_plan_operations_not_rejection(self):
        plans = self._make_plans()
        proposed = """
flowchart TD
    O["Objective: test"]
    A["Add summary method to Plan"]
    B["Add unit test for summary"]
    C["Add a brand new capability"]
    O --> A
    A --> B
    B --> C
"""
        result = canonicalize_mermaid(
            proposed, plans, objective=_objective(), allow_plan_adds=True
        )
        # Not accepted yet — requires operator approval of the add op.
        self.assertFalse(result.accepted)
        self.assertEqual(1, len(result.plan_operations))
        self.assertEqual("add", result.plan_operations[0].kind)
        self.assertEqual("Add a brand new capability", result.plan_operations[0].proposed_label)
        # Crucially, this should NOT be rejected as an orphan — it's a pending op.
        self.assertFalse(
            any("orphan proposed node" in r for r in result.rejection_reasons),
            result.rejection_reasons,
        )

    def test_rejects_broken_edge(self):
        plans = self._make_plans()
        proposed = """
flowchart TD
    O["Objective: test"]
    A["Add summary method to Plan"]
    B["Add unit test for summary"]
    O --> A
    A --> Z
"""
        result = canonicalize_mermaid(proposed, plans, objective=_objective())
        self.assertFalse(result.accepted)
        self.assertTrue(
            any("undeclared node" in r for r in result.rejection_reasons),
            result.rejection_reasons,
        )

    def test_edge_rewrite_preserves_llm_topology(self):
        plans = self._make_plans()
        # LLM proposes a NON-linear structure: both A and B depend directly on O
        proposed = """
flowchart TD
    O["Objective: test"]
    A["Add summary method to Plan"]
    B["Add unit test for summary"]
    O --> A
    O --> B
"""
        result = canonicalize_mermaid(proposed, plans, objective=_objective())
        self.assertTrue(result.accepted, msg=result.rejection_reasons)
        # Canonical text should have both plans as direct children of O.
        self.assertIn("O --> P_abc123def456", result.canonical_text)
        self.assertIn("O --> P_ghi789jkl012", result.canonical_text)
        # No spurious A->B edge.
        self.assertNotIn("P_abc123def456 --> P_ghi789jkl012", result.canonical_text)


if __name__ == "__main__":
    unittest.main()
