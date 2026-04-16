"""Tests for TRIO → scope bypass: plan.slice populates task.scope, work_orchestrator skips LLM scope."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from accruvia_harness.domain import Run, RunStatus, Task, new_id
from accruvia_harness.skills.plan_draft import (
    PlanDraftTrioSkill,
    _COMPLEXITY_VALUES,
    scope_from_plan_slice,
)


class ScopeFromPlanSliceTests(unittest.TestCase):
    def test_returns_none_for_flat_plan(self) -> None:
        self.assertIsNone(scope_from_plan_slice({"label": "flat plan"}))

    def test_derives_files_from_trio_fields(self) -> None:
        result = scope_from_plan_slice({
            "target_impl": "src/foo.py::Foo.bar",
            "target_test": "tests/test_foo.py::test_bar",
            "transformation": "Add bar method to Foo",
            "risks": ["breaks callers of old_bar"],
            "estimated_complexity": "small",
        })
        self.assertIsNotNone(result)
        self.assertEqual(["src/foo.py", "tests/test_foo.py"], result["files_to_touch"])
        self.assertEqual("Add bar method to Foo", result["approach"])
        self.assertEqual(["breaks callers of old_bar"], result["risks"])
        self.assertEqual("small", result["estimated_complexity"])
        self.assertTrue(result["trio_derived"])

    def test_impl_only_plan(self) -> None:
        result = scope_from_plan_slice({
            "target_impl": "src/a.py::func",
            "transformation": "change func",
        })
        self.assertIsNotNone(result)
        self.assertEqual(["src/a.py"], result["files_to_touch"])

    def test_defaults_complexity_to_medium(self) -> None:
        result = scope_from_plan_slice({
            "target_impl": "src/a.py::f",
            "transformation": "x",
        })
        self.assertEqual("medium", result["estimated_complexity"])


class TrioSchemaExtensionTests(unittest.TestCase):
    def test_complexity_values_constant(self) -> None:
        self.assertIn("trivial", _COMPLEXITY_VALUES)
        self.assertIn("too_large", _COMPLEXITY_VALUES)

    def test_trio_parse_response_extracts_risks_and_complexity(self) -> None:
        from accruvia_harness.skills.context import RepoInventoryProvider

        repo = RepoInventoryProvider.__new__(RepoInventoryProvider)
        repo._files = {"src/a.py"}
        repo._symbols = {"src/a.py": {"Foo"}}
        repo.impl_root_candidates = ["src/"]
        repo.test_root_candidates = ["tests/"]

        ctx = MagicMock()
        ctx.repo = repo
        skill = PlanDraftTrioSkill(context=ctx)

        import json
        raw = json.dumps({
            "plans": [{
                "local_id": "p1",
                "label": "Add feature X to module A",
                "depends_on": [],
                "target_impl": "src/a.py::Foo.x",
                "target_test": "tests/test_a.py::test_x",
                "creates_new_file": True,
                "transformation": "Add x method",
                "input_samples": [1],
                "output_samples": [2],
                "resources": [],
                "supersedes": [],
                "orphan_strategy": None,
                "orphan_acceptance_reason": None,
                "risks": ["perf regression", "API break"],
                "estimated_complexity": "large",
            }]
        })
        parsed = skill.parse_response(raw)
        plan = parsed["plans"][0]
        self.assertEqual(["perf regression", "API break"], plan["risks"])
        self.assertEqual("large", plan["estimated_complexity"])

    def test_too_large_complexity_rejected_by_validator(self) -> None:
        from accruvia_harness.skills.context import RepoInventoryProvider

        repo = RepoInventoryProvider.__new__(RepoInventoryProvider)
        repo._files = {"src/a.py"}
        repo._symbols = {"src/a.py": {"Foo"}}
        repo.impl_root_candidates = ["src/"]
        repo.test_root_candidates = ["tests/"]
        repo.file_exists = lambda p: p in repo._files
        repo.symbol_exists = lambda p, s: s in repo._symbols.get(p, set())
        repo.path_matches_impl_convention = lambda p: any(p.startswith(r) for r in repo.impl_root_candidates)
        repo.path_matches_test_convention = lambda p: any(p.startswith(r) for r in repo.test_root_candidates)

        ctx = MagicMock()
        ctx.repo = repo
        skill = PlanDraftTrioSkill(context=ctx)

        parsed = {
            "plans": [{
                "local_id": "p1",
                "label": "Big refactor",
                "depends_on": [],
                "target_impl": "src/a.py::Foo",
                "target_test": None,
                "creates_new_file": False,
                "transformation": "refactor everything",
                "input_samples": [1],
                "output_samples": [2],
                "risks": [],
                "estimated_complexity": "too_large",
                "supersedes": [],
                "orphan_strategy": None,
            }]
        }
        ok, errors = skill.validate_output(parsed)
        self.assertFalse(ok)
        self.assertTrue(any("too_large" in e for e in errors))


class WorkOrchestratorScopeBypassTests(unittest.TestCase):
    def test_trio_scope_skips_llm_call(self) -> None:
        from accruvia_harness.services.work_orchestrator import SkillsWorkOrchestrator
        from accruvia_harness.skills.base import SkillResult
        from accruvia_harness.skills.registry import SkillRegistry
        from accruvia_harness.skills.scope import ScopeSkill
        from accruvia_harness.skills.implement import ImplementSkill
        from accruvia_harness.skills.self_review import SelfReviewSkill
        from accruvia_harness.skills.validate import ValidateSkill
        from accruvia_harness.skills.diagnose import DiagnoseSkill
        from accruvia_harness.skills.commit import CommitSkill

        registry = SkillRegistry()
        for s in (ScopeSkill(), ImplementSkill(), SelfReviewSkill(), ValidateSkill(), DiagnoseSkill(), CommitSkill()):
            registry.register(s)

        orchestrator = SkillsWorkOrchestrator(
            skill_registry=registry, llm_router=MagicMock(), workspace_root=Path("/tmp"),
        )

        task = Task(
            id=new_id("task"), project_id="p", title="T", objective="o",
            scope={
                "trio_derived": True,
                "files_to_touch": ["src/a.py"],
                "approach": "do the thing",
                "risks": ["risk1"],
                "estimated_complexity": "small",
                "non_negotiables": [],
            },
        )
        run = Run(id=new_id("run"), task_id=task.id, status=RunStatus.WORKING, attempt=1, summary="")

        invoked_skills: list[str] = []

        def fake_invoke(skill, invocation, router, **kw):
            invoked_skills.append(invocation.skill_name)
            if invocation.skill_name == "scope":
                raise AssertionError("scope LLM call should NOT happen for TRIO tasks")
            if invocation.skill_name == "implement":
                return SkillResult(
                    skill_name="implement", success=True,
                    output={"edits": [], "new_files": [], "deleted_files": [], "rationale": "ok"},
                )
            if invocation.skill_name == "self_review":
                return SkillResult(
                    skill_name="self_review", success=True,
                    output={"ship_ready": True, "summary": "ok", "concerns": []},
                )
            return SkillResult(skill_name=invocation.skill_name, success=True, output={})

        with tempfile.TemporaryDirectory() as tmp:
            with patch("accruvia_harness.services.work_orchestrator.invoke_skill", side_effect=fake_invoke), \
                 patch("accruvia_harness.services.work_orchestrator._collect_repo_context", return_value=""), \
                 patch("accruvia_harness.services.work_orchestrator._load_file_contents", return_value={}), \
                 patch("accruvia_harness.services.work_orchestrator._load_reference_contents", return_value={}), \
                 patch("accruvia_harness.services.work_orchestrator._load_related_files", return_value={}), \
                 patch("accruvia_harness.services.work_orchestrator._search_codebase", return_value={}), \
                 patch("accruvia_harness.services.work_orchestrator.apply_changes", return_value={"written": ["src/a.py"], "rejected": [], "edits_applied": 1, "new_files_created": 0}), \
                 patch("accruvia_harness.services.work_orchestrator._git_diff", return_value="diff"), \
                 patch("accruvia_harness.services.work_orchestrator.CostTracker") as mock_ct:
                mock_ct.return_value.check_budget.return_value = (True, 0)
                orchestrator.execute(task, run, Path(tmp), Path(tmp) / "rd")

        self.assertNotIn("scope", invoked_skills)
        self.assertIn("implement", invoked_skills)

    def test_non_trio_task_still_calls_scope(self) -> None:
        from accruvia_harness.services.work_orchestrator import SkillsWorkOrchestrator
        from accruvia_harness.skills.base import SkillResult
        from accruvia_harness.skills.registry import SkillRegistry
        from accruvia_harness.skills.scope import ScopeSkill
        from accruvia_harness.skills.implement import ImplementSkill
        from accruvia_harness.skills.self_review import SelfReviewSkill
        from accruvia_harness.skills.validate import ValidateSkill
        from accruvia_harness.skills.diagnose import DiagnoseSkill
        from accruvia_harness.skills.commit import CommitSkill

        registry = SkillRegistry()
        for s in (ScopeSkill(), ImplementSkill(), SelfReviewSkill(), ValidateSkill(), DiagnoseSkill(), CommitSkill()):
            registry.register(s)
        orchestrator = SkillsWorkOrchestrator(
            skill_registry=registry, llm_router=MagicMock(), workspace_root=Path("/tmp"),
        )
        task = Task(
            id=new_id("task"), project_id="p", title="T", objective="o",
            scope={},
        )
        run = Run(id=new_id("run"), task_id=task.id, status=RunStatus.WORKING, attempt=1, summary="")

        invoked_skills: list[str] = []

        def fake_invoke(skill, invocation, router, **kw):
            invoked_skills.append(invocation.skill_name)
            if invocation.skill_name == "scope":
                return SkillResult(skill_name="scope", success=False, errors=["forced"])
            return SkillResult(skill_name=invocation.skill_name, success=False, errors=["x"])

        with tempfile.TemporaryDirectory() as tmp:
            with patch("accruvia_harness.services.work_orchestrator.invoke_skill", side_effect=fake_invoke), \
                 patch("accruvia_harness.services.work_orchestrator._collect_repo_context", return_value=""), \
                 patch("accruvia_harness.services.work_orchestrator._load_related_files", return_value={}), \
                 patch("accruvia_harness.services.work_orchestrator._search_codebase", return_value={}), \
                 patch("accruvia_harness.services.work_orchestrator.CostTracker") as mock_ct:
                mock_ct.return_value.check_budget.return_value = (True, 0)
                result = orchestrator.execute(task, run, Path(tmp), Path(tmp) / "rd")

        self.assertIn("scope", invoked_skills)
        self.assertEqual("failed", result.outcome)


if __name__ == "__main__":
    unittest.main()
