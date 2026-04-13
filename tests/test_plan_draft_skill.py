"""Unit tests for PlanDraftSkill and materialize_plans_from_skill_output."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from accruvia_harness.domain import Objective, ObjectiveStatus, Project, new_id
from accruvia_harness.mermaid import canonical_node_id
from accruvia_harness.skills.plan_draft import (
    PlanDraftSkill,
    materialize_plans_from_skill_output,
)
from accruvia_harness.store import SQLiteHarnessStore


_VALID_PLANS_JSON = json.dumps(
    {
        "plans": [
            {"local_id": "p1", "label": "Add domain.Run.phase field and RunPhase enum", "depends_on": []},
            {"local_id": "p2", "label": "Extract run_work() helper into RunService", "depends_on": ["p1"]},
            {"local_id": "p3", "label": "Extract run_validate() helper into RunService", "depends_on": ["p1"]},
            {"local_id": "p4", "label": "Wire run_once to drive phases sequentially", "depends_on": ["p2", "p3"]},
        ]
    }
)


class PlanDraftSkillPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = PlanDraftSkill()

    def test_prompt_includes_intent_and_non_negotiables(self):
        prompt = self.skill.build_prompt(
            {
                "objective_title": "Refactor task execution pipeline",
                "intent_summary": "Split work/validate/decide phases",
                "success_definition": "Phases run independently",
                "non_negotiables": ["No child tasks for retry"],
                "frustration_signals": [],
            }
        )
        self.assertIn("Refactor task execution pipeline", prompt)
        self.assertIn("Split work/validate/decide phases", prompt)
        self.assertIn("No child tasks for retry", prompt)
        self.assertIn("DEFINITION OF ATOMIC", prompt)
        self.assertIn("local_id", prompt)
        self.assertIn("depends_on", prompt)

    def test_prompt_renders_prior_round_findings_when_present(self):
        prompt = self.skill.build_prompt(
            {
                "objective_title": "x",
                "intent_summary": "y",
                "success_definition": "z",
                "prior_round_findings": ["p2 should depend on p1 only"],
                "round_number": 2,
            }
        )
        self.assertIn("round 2", prompt)
        self.assertIn("p2 should depend on p1 only", prompt)


class PlanDraftSkillParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = PlanDraftSkill()

    def test_valid_output_parses_cleanly(self):
        parsed = self.skill.parse_response(_VALID_PLANS_JSON)
        self.assertEqual(4, len(parsed["plans"]))
        self.assertEqual("p1", parsed["plans"][0]["local_id"])
        self.assertEqual(["p1"], parsed["plans"][1]["depends_on"])

    def test_empty_response_parses_to_empty_list(self):
        self.assertEqual({"plans": []}, self.skill.parse_response(""))
        self.assertEqual({"plans": []}, self.skill.parse_response("not json at all"))

    def test_tolerates_dependencies_alias(self):
        text = json.dumps(
            {"plans": [{"local_id": "p1", "label": "First", "dependencies": []}]}
        )
        parsed = self.skill.parse_response(text)
        self.assertEqual(1, len(parsed["plans"]))
        self.assertEqual([], parsed["plans"][0]["depends_on"])

    def test_drops_malformed_entries(self):
        text = json.dumps(
            {
                "plans": [
                    {"local_id": "p1", "label": "good one", "depends_on": []},
                    "not a dict",
                    {"local_id": "", "label": "missing id"},
                    {"local_id": "p3", "label": "", "depends_on": []},
                    {"local_id": "p4", "label": "good two", "depends_on": ["p1"]},
                ]
            }
        )
        parsed = self.skill.parse_response(text)
        self.assertEqual(2, len(parsed["plans"]))
        self.assertEqual("p1", parsed["plans"][0]["local_id"])
        self.assertEqual("p4", parsed["plans"][1]["local_id"])


class PlanDraftSkillValidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = PlanDraftSkill()

    def test_valid_output_validates(self):
        parsed = self.skill.parse_response(_VALID_PLANS_JSON)
        ok, errors = self.skill.validate_output(parsed)
        self.assertTrue(ok, msg=errors)
        self.assertEqual([], errors)

    def test_empty_list_rejected(self):
        ok, errors = self.skill.validate_output({"plans": []})
        self.assertFalse(ok)
        self.assertIn("empty", errors[0].lower())

    def test_missing_plans_field_rejected(self):
        ok, errors = self.skill.validate_output({})
        self.assertFalse(ok)

    def test_exceeds_hard_cap_rejected(self):
        plans = [
            {"local_id": f"p{i}", "label": f"plan {i}", "depends_on": []}
            for i in range(1, 17)  # 16 plans, cap is 15
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertIn("exceeds max", errors[0])

    def test_duplicate_local_id_rejected(self):
        plans = [
            {"local_id": "p1", "label": "first", "depends_on": []},
            {"local_id": "p1", "label": "duplicate", "depends_on": []},
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertIn("duplicate local_id", errors[0])

    def test_forward_reference_rejected(self):
        plans = [
            {"local_id": "p1", "label": "first", "depends_on": ["p2"]},
            {"local_id": "p2", "label": "second", "depends_on": []},
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertIn("forward or unknown reference", errors[0])

    def test_self_reference_rejected(self):
        plans = [
            {"local_id": "p1", "label": "first", "depends_on": []},
            {"local_id": "p2", "label": "circular", "depends_on": ["p2"]},
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertIn("self-reference", errors[0])

    def test_unknown_dep_reference_rejected(self):
        plans = [
            {"local_id": "p1", "label": "first", "depends_on": []},
            {"local_id": "p2", "label": "bad dep", "depends_on": ["p99"]},
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertIn("p99", errors[0])


class MaterializePlansTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteHarnessStore(Path(self.tmp.name) / "harness.db")
        self.store.initialize()
        self.store.create_project(
            Project(id=new_id("project"), name="demo", description="demo")
        )
        project = self.store.list_projects()[0]
        self.objective_id = new_id("objective")
        self.store.create_objective(
            Objective(
                id=self.objective_id,
                project_id=project.id,
                title="Test",
                summary="test",
                status=ObjectiveStatus.OPEN,
            )
        )

    def test_materialize_creates_plans_with_canonical_ids(self):
        plans_data = [
            {"local_id": "p1", "label": "First plan", "depends_on": []},
            {"local_id": "p2", "label": "Second plan", "depends_on": ["p1"]},
            {"local_id": "p3", "label": "Third plan", "depends_on": ["p1", "p2"]},
        ]
        persisted = materialize_plans_from_skill_output(
            self.store, self.objective_id, plans_data
        )
        self.assertEqual(3, len(persisted))
        for plan in persisted:
            self.assertEqual(plan.mermaid_node_id, canonical_node_id(plan))
            self.assertTrue(plan.mermaid_node_id.startswith("P_"))
            self.assertEqual("approved", plan.approval_status)

    def test_materialize_resolves_local_ids_to_plan_ids_in_dependencies(self):
        plans_data = [
            {"local_id": "p1", "label": "First plan", "depends_on": []},
            {"local_id": "p2", "label": "Second plan", "depends_on": ["p1"]},
        ]
        persisted = materialize_plans_from_skill_output(
            self.store, self.objective_id, plans_data
        )
        # p2's deps should reference p1's REAL plan.id, not "p1"
        deps = persisted[1].slice["dependencies"]
        self.assertEqual(1, len(deps))
        self.assertEqual(persisted[0].id, deps[0])
        self.assertNotEqual("p1", deps[0])

    def test_materialize_persists_to_store(self):
        plans_data = [
            {"local_id": "p1", "label": "First plan", "depends_on": []},
        ]
        persisted = materialize_plans_from_skill_output(
            self.store, self.objective_id, plans_data
        )
        stored = self.store.list_plans_for_objective(self.objective_id)
        self.assertEqual(1, len(stored))
        self.assertEqual(persisted[0].id, stored[0].id)
        self.assertEqual("First plan", stored[0].slice["label"])

    def test_materialize_tags_author(self):
        plans_data = [{"local_id": "p1", "label": "x", "depends_on": []}]
        persisted = materialize_plans_from_skill_output(
            self.store, self.objective_id, plans_data, author_tag="test_tag"
        )
        self.assertEqual("test_tag", persisted[0].slice["derived_from"])


if __name__ == "__main__":
    unittest.main()
