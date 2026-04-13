"""Tests that SelfReviewSkill enforces non-negotiables deterministically.

Rationale: during the dogfood run on objective_9533099ca701, the LLM
self-review returned ship_ready=True even though the diff violated the
intent_model's non-negotiable "Must have at least one unit test in
tests/test_plan_linkage.py" (the harness put the test in
tests/test_domain.py instead). The LLM can't be trusted to enforce
file-path constraints by semantic reading alone — we need a deterministic
post-check that overrides the verdict when a forbidden file is touched or
a required file is missing.
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from accruvia_harness.domain import IntentModel, Objective, ObjectiveStatus, new_id
from accruvia_harness.services.task_service import TaskService
from accruvia_harness.skills.self_review import SelfReviewSkill
from accruvia_harness.store import SQLiteHarnessStore


_DIFF_TOUCHING_WRONG_FILE = """diff --git a/tests/test_domain.py b/tests/test_domain.py
new file mode 100644
index 0000000..abcdef1
--- /dev/null
+++ b/tests/test_domain.py
@@ -0,0 +1,5 @@
+def test_plan_summary():
+    pass
"""

_DIFF_TOUCHING_RIGHT_FILE = """diff --git a/tests/test_plan_linkage.py b/tests/test_plan_linkage.py
index 1111111..2222222 100644
--- a/tests/test_plan_linkage.py
+++ b/tests/test_plan_linkage.py
@@ -100,3 +100,5 @@ class PlanLinkageTests(unittest.TestCase):
+    def test_plan_summary(self):
+        pass
"""


class SelfReviewEnforceNonNegotiablesTests(unittest.TestCase):
    """The LLM's ship_ready verdict must be overridden when the diff
    violates a non-negotiable that names a specific file path."""

    def setUp(self) -> None:
        self.skill = SelfReviewSkill()

    def _parsed_pass(self) -> dict:
        return {
            "issues": [],
            "ship_ready": True,
            "summary": "LGTM",
        }

    def test_required_file_missing_forces_blocker(self) -> None:
        nns = ["Must have at least one unit test in tests/test_plan_linkage.py."]
        result = self.skill.enforce_non_negotiables(
            self._parsed_pass(), nns, _DIFF_TOUCHING_WRONG_FILE,
        )
        self.assertFalse(result["ship_ready"])
        blockers = [i for i in result["issues"] if i["severity"] == "blocker"]
        self.assertTrue(blockers)
        self.assertIn("test_plan_linkage.py", blockers[0]["description"])
        self.assertIn("NON_NEGOTIABLE VIOLATED", blockers[0]["description"])

    def test_required_file_touched_passes(self) -> None:
        nns = ["Must have at least one unit test in tests/test_plan_linkage.py."]
        result = self.skill.enforce_non_negotiables(
            self._parsed_pass(), nns, _DIFF_TOUCHING_RIGHT_FILE,
        )
        self.assertTrue(result["ship_ready"])
        blockers = [i for i in result["issues"] if i["severity"] == "blocker"]
        self.assertEqual(0, len(blockers))

    def test_forbidden_file_touched_forces_blocker(self) -> None:
        nns = ["Must not modify src/accruvia_harness/domain.py."]
        diff = """diff --git a/src/accruvia_harness/domain.py b/src/accruvia_harness/domain.py
--- a/src/accruvia_harness/domain.py
+++ b/src/accruvia_harness/domain.py
@@ -1 +1,2 @@
+# added line
"""
        result = self.skill.enforce_non_negotiables(
            self._parsed_pass(), nns, diff,
        )
        self.assertFalse(result["ship_ready"])
        blockers = [i for i in result["issues"] if i["severity"] == "blocker"]
        self.assertTrue(blockers)
        self.assertIn("forbidden", blockers[0]["description"].lower())

    def test_forbidden_file_not_touched_passes(self) -> None:
        nns = ["Must not modify src/accruvia_harness/domain.py."]
        result = self.skill.enforce_non_negotiables(
            self._parsed_pass(), nns, _DIFF_TOUCHING_RIGHT_FILE,
        )
        self.assertTrue(result["ship_ready"])

    def test_empty_non_negotiables_passes_unchanged(self) -> None:
        before = self._parsed_pass()
        result = self.skill.enforce_non_negotiables(before, [], _DIFF_TOUCHING_WRONG_FILE)
        self.assertTrue(result["ship_ready"])
        self.assertEqual(before["ship_ready"], result["ship_ready"])

    def test_non_negotiable_without_file_path_is_ignored(self) -> None:
        """A non-negotiable that doesn't name a specific file path cannot be
        enforced deterministically. The heuristic should not false-positive."""
        nns = ["Must be simple and easy to understand."]
        result = self.skill.enforce_non_negotiables(
            self._parsed_pass(), nns, _DIFF_TOUCHING_WRONG_FILE,
        )
        self.assertTrue(result["ship_ready"])

    def test_prompt_contains_non_negotiables_block(self) -> None:
        prompt = self.skill.build_prompt({
            "title": "Test",
            "objective": "Test",
            "diff": _DIFF_TOUCHING_WRONG_FILE,
            "non_negotiables": ["Must put the test in tests/test_plan_linkage.py."],
        })
        self.assertIn("NON-NEGOTIABLES", prompt)
        self.assertIn("tests/test_plan_linkage.py", prompt)


class TaskServiceCopiesNonNegotiablesFromIntentModelTests(unittest.TestCase):
    """Creating a task under an objective must freeze the intent model's
    non_negotiables into task.scope so the self_review enforcement picks
    them up during execution. Without this, the contract is visible to
    the planner but invisible to the executor."""

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
                summary="test",
                status=ObjectiveStatus.OPEN,
            )
        )
        self.store.create_intent_model(
            IntentModel(
                id=new_id("intent"),
                objective_id=self.objective_id,
                version=1,
                intent_summary="test intent",
                success_definition="test done",
                non_negotiables=[
                    "Must modify src/accruvia_harness/domain.py only.",
                    "Must add a test in tests/test_plan_linkage.py.",
                ],
                current_confidence=0.9,
            )
        )

    def test_scope_carries_non_negotiables(self) -> None:
        task = self.task_service.create_task_with_policy(
            project_id=self.project.id,
            title="Test task",
            objective="do the thing",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            objective_id=self.objective_id,
            strategy="atomic_from_mermaid",
            max_attempts=3,
        )
        nns = task.scope.get("non_negotiables")
        self.assertIsNotNone(nns)
        self.assertEqual(2, len(nns))
        self.assertIn("domain.py", nns[0])
        self.assertIn("test_plan_linkage.py", nns[1])

    def test_scope_non_negotiables_survive_db_round_trip(self) -> None:
        task = self.task_service.create_task_with_policy(
            project_id=self.project.id,
            title="Test task",
            objective="do the thing",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            objective_id=self.objective_id,
            strategy="atomic_from_mermaid",
            max_attempts=3,
        )
        fetched = self.store.get_task(task.id)
        nns = fetched.scope.get("non_negotiables")
        self.assertEqual(2, len(nns))

    def test_objective_without_intent_model_yields_no_non_negotiables(self) -> None:
        bare_id = new_id("objective")
        self.store.create_objective(
            Objective(
                id=bare_id,
                project_id=self.project.id,
                title="Bare",
                summary="no intent",
                status=ObjectiveStatus.OPEN,
            )
        )
        task = self.task_service.create_task_with_policy(
            project_id=self.project.id,
            title="Bare task",
            objective="do it",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            objective_id=bare_id,
            strategy="atomic_from_mermaid",
            max_attempts=3,
        )
        self.assertNotIn("non_negotiables", task.scope)


if __name__ == "__main__":
    unittest.main()
