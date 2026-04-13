"""Unit tests for ObjectiveReviewOrchestrator."""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from accruvia_harness.services.objective_review_orchestrator import ObjectiveReviewOrchestrator
from accruvia_harness.skills.base import SkillResult


_DIMENSIONS = (
    "intent_fidelity",
    "unit_test_coverage",
    "integration_e2e_coverage",
    "security",
    "devops",
    "atomic_fidelity",
    "code_structure",
)


@dataclass
class _StubObjective:
    id: str = "obj-1"
    project_id: str = "proj-1"
    title: str = "Stub objective"
    summary: str = "Stub summary"


@dataclass
class _StubIntentModel:
    intent_summary: str = "stay safe"
    success_definition: str = "no regressions"
    non_negotiables: list[str] = field(default_factory=list)


@dataclass
class _StubMermaid:
    content: str = "flowchart TD\nA-->B"


class _StubStore:
    def __init__(self, objective: _StubObjective, tasks: list[Any]):
        self._objective = objective
        self._tasks = tasks

    def get_objective(self, _objective_id: str) -> _StubObjective:
        return self._objective

    def latest_intent_model(self, _objective_id: str) -> _StubIntentModel:
        return _StubIntentModel()

    def latest_mermaid_artifact(self, _objective_id: str, _kind: str) -> _StubMermaid:
        return _StubMermaid()

    def list_tasks(self, _project_id: str) -> list[Any]:
        return list(self._tasks)


class _StubLLMRouter:
    pass


class _RecordingRegistry:
    """Returns a single stub skill that overrides invoke_skill behaviour.

    We monkey-patch invoke_skill on the orchestrator module so the skill never
    actually runs against an LLM.
    """

    def __init__(self, skills: dict[str, Any]):
        self._skills = skills

    def get(self, name: str) -> Any:
        return self._skills[name]


class _StubSkill:
    def __init__(self, name: str, dimension: str) -> None:
        self.name = name
        self.dimension = dimension
        self.reviewer_label = f"{dimension}_reviewer"


@dataclass
class _StubTask:
    id: str
    project_id: str
    objective_id: str | None
    title: str = "stub"


class ObjectiveReviewOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.workspace_root = Path(self.tmpdir.name)
        self.objective = _StubObjective()
        self.task = _StubTask(id="task-1", project_id="proj-1", objective_id="obj-1")
        self.store = _StubStore(self.objective, [self.task])
        self.skills = {
            f"review_{dim}": _StubSkill(f"review_{dim}", dim) for dim in _DIMENSIONS
        }
        self.registry = _RecordingRegistry(self.skills)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _patch_invoke(self, results: dict[str, SkillResult]):
        from accruvia_harness.services import objective_review_orchestrator as mod

        def _fake_invoke(skill, invocation, llm_router, telemetry=None):
            return results[skill.name]

        self._original_invoke = mod.invoke_skill
        mod.invoke_skill = _fake_invoke

    def _restore_invoke(self):
        from accruvia_harness.services import objective_review_orchestrator as mod

        mod.invoke_skill = self._original_invoke

    def test_all_pass_returns_seven_pass_packets(self) -> None:
        results = {
            f"review_{dim}": SkillResult(
                skill_name=f"review_{dim}",
                success=True,
                output={
                    "dimension": dim,
                    "verdict": "pass",
                    "summary": f"{dim} ok",
                    "findings": [],
                },
            )
            for dim in _DIMENSIONS
        }
        self._patch_invoke(results)
        try:
            orch = ObjectiveReviewOrchestrator(
                skill_registry=self.registry,
                llm_router=_StubLLMRouter(),
                store=self.store,
                workspace_root=self.workspace_root,
            )
            outcome = orch.execute(self.objective.id, "review-1")
        finally:
            self._restore_invoke()
        self.assertEqual(7, len(outcome["packets"]))
        self.assertTrue(outcome["review_clear"])
        self.assertEqual(0, outcome["failed_count"])
        self.assertEqual(
            {dim for dim in _DIMENSIONS},
            {p["dimension"] for p in outcome["packets"]},
        )

    def test_failed_reviewer_emits_stub_packet(self) -> None:
        results = {
            f"review_{dim}": SkillResult(
                skill_name=f"review_{dim}",
                success=True,
                output={"dimension": dim, "verdict": "pass", "summary": "ok", "findings": []},
            )
            for dim in _DIMENSIONS
        }
        results["review_security"] = SkillResult(
            skill_name="review_security",
            success=False,
            errors=["llm_execution_failed: timeout"],
        )
        self._patch_invoke(results)
        try:
            orch = ObjectiveReviewOrchestrator(
                skill_registry=self.registry,
                llm_router=_StubLLMRouter(),
                store=self.store,
                workspace_root=self.workspace_root,
            )
            outcome = orch.execute(self.objective.id, "review-2")
        finally:
            self._restore_invoke()
        self.assertEqual(7, len(outcome["packets"]))
        self.assertEqual(1, outcome["failed_count"])
        self.assertFalse(outcome["review_clear"])
        security_packet = next(p for p in outcome["packets"] if p["dimension"] == "security")
        self.assertEqual("remediation_required", security_packet["verdict"])
        self.assertIn("llm_execution_failed", security_packet["findings"][0])
        self.assertEqual("report", security_packet["required_artifact_type"])

    def test_concern_verdict_carries_evidence_contract(self) -> None:
        results = {
            f"review_{dim}": SkillResult(
                skill_name=f"review_{dim}",
                success=True,
                output={"dimension": dim, "verdict": "pass", "summary": "ok", "findings": []},
            )
            for dim in _DIMENSIONS
        }
        results["review_unit_test_coverage"] = SkillResult(
            skill_name="review_unit_test_coverage",
            success=True,
            output={
                "dimension": "unit_test_coverage",
                "verdict": "concern",
                "summary": "Need more unit tests.",
                "findings": ["Missing tests for gate function."],
                "evidence": ["src/validate.py changed without test"],
                "severity": "medium",
                "owner_scope": "tests",
                "required_artifact_type": "test_execution_report",
                "closure_criteria": "All gate paths must have unit tests.",
                "evidence_required": "test report covering gate function",
            },
        )
        self._patch_invoke(results)
        try:
            orch = ObjectiveReviewOrchestrator(
                skill_registry=self.registry,
                llm_router=_StubLLMRouter(),
                store=self.store,
                workspace_root=self.workspace_root,
            )
            outcome = orch.execute(self.objective.id, "review-3")
        finally:
            self._restore_invoke()
        self.assertFalse(outcome["review_clear"])
        utc_packet = next(p for p in outcome["packets"] if p["dimension"] == "unit_test_coverage")
        self.assertEqual("concern", utc_packet["verdict"])
        self.assertEqual("test_execution_report", utc_packet["required_artifact_type"])
        self.assertEqual("test_execution_report", utc_packet["evidence_contract"]["required_artifact_type"])
        self.assertEqual("tests", utc_packet["owner_scope"])
        self.assertTrue(utc_packet["findings"])


if __name__ == "__main__":
    unittest.main()
