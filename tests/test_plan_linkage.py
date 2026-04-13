from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import Objective, ObjectiveStatus, new_id
from accruvia_harness.services.task_service import TaskService
from accruvia_harness.store import SQLiteHarnessStore


class PlanLinkageTests(unittest.TestCase):
    """Enforce the objective -> plan -> task lineage contract.

    Every task created against an objective must carry a plan_id and a
    stable mermaid_node_id. The plan row must exist in the plans table and
    reference the same node id. Tasks without an objective_id are skipped
    (plans are objective-scoped).

    See specs/atomic-plan-schema.md and specs/plan-to-task-mapping.md for
    the intended lineage, and the backfill script bin/accruvia-backfill-plans
    for the migration path from the unlinked historical state.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteHarnessStore(Path(self.tmp.name) / "harness.db")
        self.store.initialize()
        self.task_service = TaskService(self.store)
        self.project = self.task_service.create_project("demo", "demo")
        self.objective_id = new_id("objective")
        self.store.create_objective(
            Objective(
                id=self.objective_id,
                project_id=self.project.id,
                title="Test objective",
                summary="test summary",
                status=ObjectiveStatus.OPEN,
            )
        )

    _UNSET = object()

    def _create_task(self, *, title: str = "Test task", objective_id=_UNSET):
        linked = self.objective_id if objective_id is self._UNSET else objective_id
        return self.task_service.create_task_with_policy(
            project_id=self.project.id,
            title=title,
            objective="do the thing",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            objective_id=linked,
            strategy="atomic_from_mermaid",
            max_attempts=3,
        )

    def test_new_task_has_plan_and_node_id(self) -> None:
        """A newly-created task must carry a plan_id and a canonical
        mermaid_node_id derived from that plan via canonical_node_id().

        Prefix changed from T_ to P_ when Model C replaced the synthetic
        task-id-derived fallback with the canonical plan-derived id. The
        P_ prefix visually marks plan-anchored nodes in rendered diagrams.
        """
        from accruvia_harness.mermaid import canonical_node_id

        task = self._create_task()
        self.assertIsNotNone(task.plan_id)
        self.assertIsNotNone(task.mermaid_node_id)
        self.assertTrue(
            task.mermaid_node_id.startswith("P_"),
            f"expected canonical P_ prefix, got {task.mermaid_node_id}",
        )
        # The invariant: task.mermaid_node_id == canonical_node_id(plan)
        plan = self.store.get_plan(task.plan_id)
        self.assertEqual(canonical_node_id(plan), task.mermaid_node_id)
        self.assertEqual(plan.mermaid_node_id, task.mermaid_node_id)

    def test_plan_row_is_persisted_with_matching_node(self) -> None:
        task = self._create_task()
        plan = self.store.get_plan(task.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.objective_id, self.objective_id)
        self.assertEqual(plan.mermaid_node_id, task.mermaid_node_id)
        self.assertEqual(plan.approval_status, "approved")

    def test_task_round_trips_plan_fields_through_db(self) -> None:
        task = self._create_task()
        fetched = self.store.get_task(task.id)
        self.assertEqual(fetched.plan_id, task.plan_id)
        self.assertEqual(fetched.mermaid_node_id, task.mermaid_node_id)

    def test_each_task_gets_its_own_plan(self) -> None:
        t1 = self._create_task(title="Task 1")
        t2 = self._create_task(title="Task 2")
        self.assertNotEqual(t1.plan_id, t2.plan_id)
        self.assertNotEqual(t1.mermaid_node_id, t2.mermaid_node_id)

    def test_task_without_objective_skips_plan(self) -> None:
        task = self._create_task(objective_id=None)
        self.assertIsNone(task.plan_id)
        self.assertIsNone(task.mermaid_node_id)

    def test_list_plans_for_objective_returns_created_plans(self) -> None:
        t1 = self._create_task(title="A")
        t2 = self._create_task(title="B")
        plans = self.store.list_plans_for_objective(self.objective_id)
        plan_ids = {p.id for p in plans}
        self.assertIn(t1.plan_id, plan_ids)
        self.assertIn(t2.plan_id, plan_ids)

    def test_get_plan_by_node_returns_same_row(self) -> None:
        task = self._create_task()
        plan = self.store.get_plan_by_node(self.objective_id, task.mermaid_node_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.id, task.plan_id)

    def test_explicit_mermaid_node_id_is_overwritten_by_canonical(self) -> None:
        """Under Model C, an explicit mermaid_node_id passed into
        create_task_with_policy is NOT honored — it is overwritten by
        canonical_node_id(plan) so that the invariant
        task.mermaid_node_id == plan.mermaid_node_id == P_<hash> holds
        unconditionally.

        The old contract (pre-Model C) was that atomic decomposition
        could pass flowchart aliases like 'A', 'B' and have them stored
        verbatim. That contract created the exact bug Query #3 found:
        when the mermaid was regenerated (or LLM-rewritten) the node id
        'A' no longer referred to any node, and the join between
        tasks.mermaid_node_id and mermaid_artifacts.content broke.

        The new contract: node id derivation is plan-scoped and
        deterministic. LLM/operator Mermaid proposals that use different
        aliases get normalized by canonicalize_mermaid() before
        persistence; they never reach task creation with their own ids.
        """
        from accruvia_harness.mermaid import canonical_node_id

        task = self.task_service.create_task_with_policy(
            project_id=self.project.id,
            title="Task with doomed explicit id",
            objective="test",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            objective_id=self.objective_id,
            strategy="atomic_from_mermaid",
            max_attempts=3,
            mermaid_node_id="A",  # will be ignored
        )
        # The 'A' was overwritten; the stored id is canonical.
        self.assertNotEqual("A", task.mermaid_node_id)
        self.assertTrue(task.mermaid_node_id.startswith("P_"))
        plan = self.store.get_plan(task.plan_id)
        self.assertEqual(canonical_node_id(plan), task.mermaid_node_id)
        self.assertEqual(plan.mermaid_node_id, task.mermaid_node_id)


if __name__ == "__main__":
    unittest.main()
