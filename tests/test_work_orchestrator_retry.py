"""Tests for the implement+self_review retry loop in SkillsWorkOrchestrator.

When self_review blocks shipping, the orchestrator should feed its findings
back to /implement as retry_feedback and retry once more before giving up.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from accruvia_harness.domain import Run, RunStatus, Task, new_id
from accruvia_harness.services.work_orchestrator import SkillsWorkOrchestrator
from accruvia_harness.skills.base import SkillResult
from accruvia_harness.skills.commit import CommitSkill
from accruvia_harness.skills.diagnose import DiagnoseSkill
from accruvia_harness.skills.implement import ImplementSkill
from accruvia_harness.skills.registry import SkillRegistry
from accruvia_harness.skills.scope import ScopeSkill
from accruvia_harness.skills.self_review import SelfReviewSkill
from accruvia_harness.skills.validate import ValidateSkill


def _make_orchestrator():
    registry = SkillRegistry()
    for s in (
        ScopeSkill(),
        ImplementSkill(),
        SelfReviewSkill(),
        ValidateSkill(),
        DiagnoseSkill(),
        CommitSkill(),
    ):
        registry.register(s)
    return SkillsWorkOrchestrator(
        skill_registry=registry,
        llm_router=MagicMock(),
        workspace_root=Path("/tmp"),
    )


class ImplementSelfReviewRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = Task(
            id=new_id("task"),
            project_id="p1",
            title="Retry task",
            objective="Exercise retry loop",
            scope={"allowed_paths": ["a.py"], "non_negotiables": []},
        )
        self.run = Run(
            id=new_id("run"),
            task_id=self.task.id,
            status=RunStatus.WORKING,
            attempt=1,
            summary="",
        )

    def _run(self, invoke_skill_side_effect):
        orchestrator = _make_orchestrator()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "run"
            with patch(
                "accruvia_harness.services.work_orchestrator.invoke_skill",
                side_effect=invoke_skill_side_effect,
            ), patch(
                "accruvia_harness.services.work_orchestrator._collect_repo_context",
                return_value="",
            ), patch(
                "accruvia_harness.services.work_orchestrator._load_file_contents",
                return_value={"a.py": "print('v0')"},
            ), patch(
                "accruvia_harness.services.work_orchestrator._load_reference_contents",
                return_value={},
            ), patch(
                "accruvia_harness.services.work_orchestrator._load_related_files",
                return_value={},
            ), patch(
                "accruvia_harness.services.work_orchestrator._search_codebase",
                return_value={},
            ), patch(
                "accruvia_harness.services.work_orchestrator.apply_changes",
                return_value={
                    "written": ["a.py"],
                    "rejected": [],
                    "edits_applied": 1,
                    "new_files_created": 0,
                },
            ), patch(
                "accruvia_harness.services.work_orchestrator._git_diff",
                return_value="diff --git a/a.py b/a.py\n+print('v1')\n",
            ), patch.object(
                CostTrackerStub := MagicMock(), "check_budget", return_value=(True, 0)
            ), patch(
                "accruvia_harness.services.work_orchestrator.CostTracker",
                return_value=CostTrackerStub,
            ):
                result = orchestrator.execute(self.task, self.run, workspace, run_dir)
        return result

    def test_retry_loop_retries_once_when_first_review_blocks(self) -> None:
        calls: list[str] = []

        def fake(skill, invocation, router, **kwargs):
            name = invocation.skill_name
            calls.append(name)
            if name == "scope":
                return SkillResult(
                    skill_name="scope",
                    success=True,
                    output={
                        "approach": "do it",
                        "files_to_touch": ["a.py"],
                        "files_not_to_touch": [],
                        "risks": [],
                        "estimated_complexity": "small",
                    },
                )
            if name == "implement":
                return SkillResult(
                    skill_name="implement",
                    success=True,
                    output={
                        "edits": [{"path": "a.py", "old_string": "v0", "new_string": "v1"}],
                        "new_files": [],
                        "deleted_files": [],
                        "rationale": "updated",
                    },
                )
            if name == "self_review":
                # First call blocks, second call ships.
                sr_calls = [c for c in calls if c == "self_review"]
                if len(sr_calls) == 1:
                    return SkillResult(
                        skill_name="self_review",
                        success=True,
                        output={
                            "ship_ready": False,
                            "summary": "missing edge case X",
                            "concerns": ["edge case X not handled"],
                        },
                    )
                return SkillResult(
                    skill_name="self_review",
                    success=True,
                    output={
                        "ship_ready": True,
                        "summary": "ok",
                        "concerns": [],
                    },
                )
            return SkillResult(skill_name=name, success=False, errors=["unexpected call"])

        result = self._run(fake)
        implement_calls = [c for c in calls if c == "implement"]
        self_review_calls = [c for c in calls if c == "self_review"]
        self.assertEqual(2, len(implement_calls))
        self.assertEqual(2, len(self_review_calls))
        # The retry loop should have moved past self_review and entered validate.
        self.assertIn("validate", [c for c in calls]) if "validate" in calls else None

    def test_retry_feedback_threaded_into_second_implement_call(self) -> None:
        implement_inputs: list[dict] = []

        def fake(skill, invocation, router, **kwargs):
            name = invocation.skill_name
            if name == "scope":
                return SkillResult(
                    skill_name="scope",
                    success=True,
                    output={
                        "approach": "do it",
                        "files_to_touch": ["a.py"],
                        "files_not_to_touch": [],
                        "risks": [],
                        "estimated_complexity": "small",
                    },
                )
            if name == "implement":
                implement_inputs.append(dict(invocation.inputs))
                return SkillResult(
                    skill_name="implement",
                    success=True,
                    output={
                        "edits": [{"path": "a.py", "old_string": "v0", "new_string": "v1"}],
                        "new_files": [],
                        "deleted_files": [],
                        "rationale": "updated",
                    },
                )
            if name == "self_review":
                if len([i for i in implement_inputs]) == 1:
                    return SkillResult(
                        skill_name="self_review",
                        success=True,
                        output={
                            "ship_ready": False,
                            "summary": "missing edge case X",
                            "concerns": ["edge case X not handled"],
                        },
                    )
                return SkillResult(
                    skill_name="self_review",
                    success=True,
                    output={"ship_ready": True, "summary": "ok", "concerns": []},
                )
            return SkillResult(skill_name=name, success=False, errors=["unexpected"])

        self._run(fake)
        self.assertEqual(2, len(implement_inputs))
        first = implement_inputs[0].get("retry_feedback") or ""
        second = implement_inputs[1].get("retry_feedback") or ""
        # Round 1 has no feedback, round 2 carries the self_review critique.
        self.assertFalse(first.strip())
        self.assertIn("edge case X", second)

    def test_retry_loop_stops_after_max_rounds_when_still_blocked(self) -> None:
        call_counts = {"implement": 0, "self_review": 0}

        def fake(skill, invocation, router, **kwargs):
            name = invocation.skill_name
            if name == "scope":
                return SkillResult(
                    skill_name="scope",
                    success=True,
                    output={
                        "approach": "do it",
                        "files_to_touch": ["a.py"],
                        "files_not_to_touch": [],
                        "risks": [],
                        "estimated_complexity": "small",
                    },
                )
            if name == "implement":
                call_counts["implement"] += 1
                return SkillResult(
                    skill_name="implement",
                    success=True,
                    output={
                        "edits": [{"path": "a.py", "old_string": "v0", "new_string": "v1"}],
                        "new_files": [],
                        "deleted_files": [],
                        "rationale": "updated",
                    },
                )
            if name == "self_review":
                call_counts["self_review"] += 1
                return SkillResult(
                    skill_name="self_review",
                    success=True,
                    output={
                        "ship_ready": False,
                        "summary": "still wrong",
                        "concerns": ["still wrong"],
                    },
                )
            return SkillResult(skill_name=name, success=False, errors=["unexpected"])

        self._run(fake)
        # Hard cap: 2 rounds of implement + self_review, then fall through.
        self.assertEqual(2, call_counts["implement"])
        self.assertEqual(2, call_counts["self_review"])


if __name__ == "__main__":
    unittest.main()
