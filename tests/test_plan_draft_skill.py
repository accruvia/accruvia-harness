"""Unit tests for PlanDraftSkill and materialize_plans_from_skill_output."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from accruvia_harness.domain import Objective, ObjectiveStatus, Project, new_id
from accruvia_harness.mermaid import canonical_node_id
from accruvia_harness.skills.context import RepoInventoryProvider, SkillContext
from accruvia_harness.skills.plan_draft import (
    PlanDraftSkill,
    PlanDraftTrioSkill,
    materialize_plans_from_skill_output,
)
from accruvia_harness.store import SQLiteHarnessStore


class _FakeRepoInventoryProvider(RepoInventoryProvider):
    """Test double: hand-seeded file set, no git ls-files call."""

    def __init__(
        self,
        files: set[str],
        impl_roots: tuple[str, ...] = ("src/accruvia_harness/",),
        test_roots: tuple[str, ...] = ("tests/",),
    ) -> None:
        super().__init__(Path("/tmp"))  # dummy root, never accessed
        self._files = set(files)
        self._impl_roots = impl_roots
        self._test_roots = test_roots

    @property
    def impl_root_candidates(self) -> tuple[str, ...]:  # type: ignore[override]
        return self._impl_roots

    @property
    def test_root_candidates(self) -> tuple[str, ...]:  # type: ignore[override]
        return self._test_roots

    def symbols_in_file(self, path: str) -> set[str]:  # type: ignore[override]
        return set()  # tests don't need real symbol extraction

    def get_prompt_block(self, focus_prefixes: tuple[str, ...] = ()) -> str:  # type: ignore[override]
        files = sorted(self.files)
        return "REPOSITORY INVENTORY (existing files):\n" + "\n".join(f"  {f}" for f in files)


def _fake_skill_context(
    files: set[str] | None = None,
    impl_roots: tuple[str, ...] = ("src/accruvia_harness/",),
    test_roots: tuple[str, ...] = ("tests/",),
) -> SkillContext:
    provider = _FakeRepoInventoryProvider(files or set(), impl_roots, test_roots)
    return SkillContext(repo=provider)


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


_VALID_TRIO_PLANS_JSON = json.dumps(
    {
        "plans": [
            {
                "local_id": "p1",
                "label": "Add Plan.summary() returning one-line repr",
                "depends_on": [],
                "target_impl": "src/accruvia_harness/domain.py::Plan.summary",
                "target_test": "tests/test_domain.py::test_plan_summary",
                "transformation": "Return a formatted string of id, objective_id, status",
                "input_samples": [
                    {"id": "plan_abc", "objective_id": "obj_xyz", "status": "approved"}
                ],
                "output_samples": ["plan_abc -> obj_xyz (approved)"],
                "resources": [],
            },
            {
                "local_id": "p2",
                "label": "Add Plan.summary() usage in bench view",
                "depends_on": ["p1"],
                "target_impl": "bin/accruvia-objective-bench",
                "target_test": "tests/test_bench.py::test_bench_shows_plan_summary",
                "transformation": "Call plan.summary() for each plan and print",
                "input_samples": [{"plan_count": 3}],
                "output_samples": ["3 plan summaries printed"],
            },
        ]
    }
)


class PlanDraftTrioSkillTests(unittest.TestCase):
    """PlanDraftTrioSkill now requires a SkillContext. Every test builds a
    fake context pre-seeded with the files its plan fixtures reference."""

    # Common seeded file set used across most tests. Includes the files
    # referenced by _VALID_TRIO_PLANS_JSON plus the hand-written fixtures.
    _SEEDED_FILES = {
        "src/accruvia_harness/domain.py",
        "tests/test_domain.py",
        "bin/accruvia-objective-bench",
        "tests/test_bench.py",
        "src/foo.py",
        "tests/test_foo.py",
        "tests/test_existing.py",
        "src/a.py",
        "src/b.py",
        "tests/test_shared.py",
    }

    def setUp(self) -> None:
        self.context = _fake_skill_context(
            files=self._SEEDED_FILES,
            impl_roots=("src/accruvia_harness/", "src/", "bin/"),
            test_roots=("tests/",),
        )
        self.skill = PlanDraftTrioSkill(context=self.context)

    def test_requires_context_at_construction(self):
        with self.assertRaises(ValueError) as cm:
            PlanDraftTrioSkill()
        self.assertIn("requires a SkillContext", str(cm.exception))

    def test_registered_with_trio_name(self):
        self.assertEqual("plan_draft_trio", self.skill.name)

    def test_prompt_includes_trio_instructions_and_inventory(self):
        prompt = self.skill.build_prompt(
            {
                "objective_title": "x",
                "intent_summary": "y",
                "success_definition": "z",
            }
        )
        # Base prompt still there
        self.assertIn("DEFINITION OF ATOMIC", prompt)
        # TRIO addendum
        self.assertIn("target_impl", prompt)
        self.assertIn("target_test", prompt)
        self.assertIn("transformation", prompt)
        self.assertIn("input_samples", prompt)
        self.assertIn("output_samples", prompt)
        self.assertIn("creates_new_file", prompt)
        # Repo inventory rendered
        self.assertIn("REPOSITORY INVENTORY", prompt)
        self.assertIn("src/accruvia_harness/domain.py", prompt)
        # Hallucination warning
        self.assertIn("Hallucinated paths", prompt)

    def test_valid_trio_output_parses_and_validates(self):
        parsed = self.skill.parse_response(_VALID_TRIO_PLANS_JSON)
        self.assertEqual(2, len(parsed["plans"]))
        self.assertEqual("p1", parsed["plans"][0]["local_id"])
        self.assertEqual(
            "src/accruvia_harness/domain.py::Plan.summary",
            parsed["plans"][0]["target_impl"],
        )
        ok, errors = self.skill.validate_output(parsed)
        self.assertTrue(ok, msg=errors)

    def test_rejects_hallucinated_target_impl_path(self):
        """A plan referencing a file not in the repo and not marked as
        creates_new_file must be rejected as hallucination."""
        plans = [
            {
                "local_id": "p1",
                "label": "Invent a fictional file",
                "depends_on": [],
                "target_impl": "src/accruvia_harness/invented.py::Nonexistent",
                "target_test": "tests/test_domain.py::test_something",
                "transformation": "Do something",
                "input_samples": [{"a": 1}],
                "output_samples": [{"b": 2}],
                "creates_new_file": False,
            }
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertTrue(
            any("Hallucinated path" in e and "invented.py" in e for e in errors),
            errors,
        )

    def test_rejects_hallucinated_target_test_path(self):
        plans = [
            {
                "local_id": "p1",
                "label": "Test against ghost test file",
                "depends_on": [],
                "target_impl": "src/accruvia_harness/domain.py::Plan",
                "target_test": "tests/test_phantom.py::test_ghost",
                "transformation": "Do something",
                "input_samples": [{"a": 1}],
                "output_samples": [{"b": 2}],
                "creates_new_file": False,
            }
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertTrue(
            any("Hallucinated path" in e and "test_phantom.py" in e for e in errors),
            errors,
        )

    def test_creates_new_file_true_allows_nonexistent_path_under_impl_root(self):
        plans = [
            {
                "local_id": "p1",
                "label": "Legitimately create a new module",
                "depends_on": [],
                "target_impl": "src/accruvia_harness/new_module.py::NewClass",
                "target_test": "tests/test_new_module.py::test_new_class",
                "transformation": "Introduce a new module under src",
                "input_samples": [{"x": 1}],
                "output_samples": [{"y": 2}],
                "creates_new_file": True,
            }
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertTrue(ok, msg=errors)

    def test_creates_new_file_true_rejected_outside_impl_conventions(self):
        """creates_new_file=true still fails if the path is outside the
        project's impl-root convention (e.g. under /tmp or a wild path)."""
        plans = [
            {
                "local_id": "p1",
                "label": "Illegal new file location",
                "depends_on": [],
                "target_impl": "/etc/passwd::hack",
                "target_test": "tests/test_x.py::test_y",  # prevent duplicate error
                "transformation": "Do something",
                "input_samples": [{}],
                "output_samples": [{}],
                "creates_new_file": True,
            }
        ]
        # Need a test-root file in the inventory
        self.context.repo._files.add("tests/test_x.py")
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertTrue(
            any("does not match an impl-root convention" in e for e in errors),
            errors,
        )

    def test_rejects_plan_with_neither_impl_nor_test(self):
        plans = [
            {
                "local_id": "p1",
                "label": "Orphan plan",
                "depends_on": [],
                "transformation": "x",
                "input_samples": [{"a": 1}],
                "output_samples": [{"b": 2}],
            }
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertTrue(any("neither target_impl nor target_test" in e for e in errors))

    def test_rejects_duplicate_target_impl_across_plans(self):
        plans = [
            {
                "local_id": "p1",
                "label": "First",
                "depends_on": [],
                "target_impl": "src/foo.py::bar",
                "target_test": "tests/test_foo.py::test_bar_a",
                "transformation": "do a",
                "input_samples": [{}],
                "output_samples": [{}],
            },
            {
                "local_id": "p2",
                "label": "Second",
                "depends_on": [],
                "target_impl": "src/foo.py::bar",  # collision
                "target_test": "tests/test_shared.py::test_bar_b",
                "transformation": "do b",
                "input_samples": [{}],
                "output_samples": [{}],
            },
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertTrue(any("already claimed" in e for e in errors))

    def test_rejects_duplicate_target_test_across_plans(self):
        plans = [
            {
                "local_id": "p1",
                "label": "First",
                "depends_on": [],
                "target_impl": "src/a.py",
                "target_test": "tests/test_shared.py::test_shared",
                "transformation": "do a",
                "input_samples": [{}],
                "output_samples": [{}],
            },
            {
                "local_id": "p2",
                "label": "Second",
                "depends_on": [],
                "target_impl": "src/b.py",
                "target_test": "tests/test_shared.py::test_shared",  # collision
                "transformation": "do b",
                "input_samples": [{}],
                "output_samples": [{}],
            },
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertTrue(any("target_test" in e and "already claimed" in e for e in errors))

    def test_rejects_missing_transformation(self):
        plans = [
            {
                "local_id": "p1",
                "label": "x",
                "depends_on": [],
                "target_impl": "src/a.py",
                "transformation": "",
                "input_samples": [{}],
                "output_samples": [{}],
            }
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertTrue(any("missing transformation" in e for e in errors))

    def test_rejects_empty_input_samples(self):
        plans = [
            {
                "local_id": "p1",
                "label": "x",
                "depends_on": [],
                "target_impl": "src/a.py",
                "transformation": "do a",
                "input_samples": [],
                "output_samples": [],
            }
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertTrue(any("input_samples" in e and "non-empty" in e for e in errors))

    def test_rejects_input_output_sample_length_mismatch(self):
        plans = [
            {
                "local_id": "p1",
                "label": "x",
                "depends_on": [],
                "target_impl": "src/a.py",
                "transformation": "do a",
                "input_samples": [{"a": 1}, {"a": 2}],
                "output_samples": [{"b": 1}],
            }
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertFalse(ok)
        self.assertTrue(any("length mismatch" in e for e in errors))

    def test_test_only_plan_is_allowed(self):
        plans = [
            {
                "local_id": "p1",
                "label": "Add test for existing function",
                "depends_on": [],
                "target_test": "tests/test_existing.py::test_new_case",
                "transformation": "Assert existing function handles edge case",
                "input_samples": [{"x": 0}],
                "output_samples": [{"raises": "ValueError"}],
            }
        ]
        ok, errors = self.skill.validate_output({"plans": plans})
        self.assertTrue(ok, msg=errors)

    def test_materialize_persists_trio_fields_into_slice(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SQLiteHarnessStore(Path(tmp.name) / "harness.db")
        store.initialize()
        store.create_project(
            Project(id=new_id("project"), name="demo", description="")
        )
        project = store.list_projects()[0]
        objective_id = new_id("objective")
        store.create_objective(
            Objective(
                id=objective_id,
                project_id=project.id,
                title="Test",
                summary="test",
                status=ObjectiveStatus.OPEN,
            )
        )

        plans_data = [
            {
                "local_id": "p1",
                "label": "Add X",
                "depends_on": [],
                "target_impl": "src/foo.py::bar",
                "target_test": "tests/test_foo.py::test_bar",
                "transformation": "Return 42",
                "input_samples": [{"a": 1}],
                "output_samples": [42],
                "resources": ["numpy"],
            },
        ]
        persisted = materialize_plans_from_skill_output(
            store, objective_id, plans_data, author_tag="plan_draft_trio"
        )
        self.assertEqual(1, len(persisted))
        slice_ = persisted[0].slice
        self.assertEqual("src/foo.py::bar", slice_["target_impl"])
        self.assertEqual("tests/test_foo.py::test_bar", slice_["target_test"])
        self.assertEqual("Return 42", slice_["transformation"])
        self.assertEqual([{"a": 1}], slice_["input_samples"])
        self.assertEqual([42], slice_["output_samples"])
        self.assertEqual(["numpy"], slice_["resources"])
        # And a round-trip through the store preserves them
        stored = store.list_plans_for_objective(objective_id)[0]
        self.assertEqual("src/foo.py::bar", stored.slice["target_impl"])


if __name__ == "__main__":
    unittest.main()
