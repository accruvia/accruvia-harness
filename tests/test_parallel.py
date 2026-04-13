from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import Project, Task, TaskStatus, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.policy import WorkResult
from accruvia_harness.store import SQLiteHarnessStore
from accruvia_harness.workers import LocalArtifactWorker


class MissingArtifactWorker(LocalArtifactWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text("plan only\n", encoding="utf-8")
        return WorkResult(
            summary="Recorded only a partial artifact set.",
            artifacts=[("plan", str(plan_path), "Plan artifact only")],
        )


class ConcurrencyLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(base / "harness.db")
        self.store.initialize()
        self.engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=self.store,
            workspace_root=base / "workspace",
        )

    def test_concurrency_limit_zero_means_unlimited(self) -> None:
        project = Project(
            id=new_id("project"),
            name="unlimited",
            description="No concurrency limit",
            max_concurrent_tasks=0,
        )
        self.store.create_project(project)
        for i in range(3):
            self.engine.create_task_with_policy(
                project_id=project.id,
                title=f"Task {i}",
                objective=f"Do thing {i}",
                priority=100,
                parent_task_id=None,
                source_run_id=None,
                external_ref_type=None,
                external_ref_id=None,
            )

        leased = []
        for i in range(3):
            task = self.store.acquire_task_lease(f"worker-{i}", 300)
            if task is not None:
                leased.append(task)

        self.assertEqual(3, len(leased))

    def test_concurrency_limit_enforced_on_lease_acquisition(self) -> None:
        project = Project(
            id=new_id("project"),
            name="limited",
            description="Concurrency capped at 1",
            max_concurrent_tasks=1,
        )
        self.store.create_project(project)
        for i in range(3):
            self.engine.create_task_with_policy(
                project_id=project.id,
                title=f"Task {i}",
                objective=f"Do thing {i}",
                priority=100 + i,
                parent_task_id=None,
                source_run_id=None,
                external_ref_type=None,
                external_ref_id=None,
            )

        first = self.store.acquire_task_lease("worker-a", 300)
        second = self.store.acquire_task_lease("worker-b", 300)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_concurrency_limit_allows_after_release(self) -> None:
        project = Project(
            id=new_id("project"),
            name="release-test",
            description="Cap at 1, release lets next in",
            max_concurrent_tasks=1,
        )
        self.store.create_project(project)
        for i in range(2):
            self.engine.create_task_with_policy(
                project_id=project.id,
                title=f"Task {i}",
                objective=f"Do thing {i}",
                priority=100,
                parent_task_id=None,
                source_run_id=None,
                external_ref_type=None,
                external_ref_id=None,
            )

        first = self.store.acquire_task_lease("worker-a", 300)
        self.assertIsNotNone(first)
        self.store.release_task_lease(first.id, "worker-a")

        second = self.store.acquire_task_lease("worker-b", 300)
        self.assertIsNotNone(second)

    def test_concurrency_limit_per_project_is_independent(self) -> None:
        project_a = Project(
            id=new_id("project"),
            name="project-a",
            description="Cap at 1",
            max_concurrent_tasks=1,
        )
        project_b = Project(
            id=new_id("project"),
            name="project-b",
            description="Unlimited",
            max_concurrent_tasks=0,
        )
        self.store.create_project(project_a)
        self.store.create_project(project_b)
        self.engine.create_task_with_policy(
            project_id=project_a.id, title="A1", objective="A work",
            priority=100, parent_task_id=None, source_run_id=None,
            external_ref_type=None, external_ref_id=None,
        )
        self.engine.create_task_with_policy(
            project_id=project_b.id, title="B1", objective="B work",
            priority=100, parent_task_id=None, source_run_id=None,
            external_ref_type=None, external_ref_id=None,
        )

        first = self.store.acquire_task_lease("worker-a", 300)
        second = self.store.acquire_task_lease("worker-b", 300)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.project_id, second.project_id)


class SpeculativeBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(base / "harness.db")
        self.store.initialize()
        self.project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(self.project)

    def test_create_branches_produces_parallel_runs(self) -> None:
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace",
        )
        task = engine.create_task_with_policy(
            project_id=self.project.id,
            title="Branching task",
            objective="Test speculative branching",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            max_attempts=1,
            max_branches=3,
        )

        result = engine.create_branches(task.id)

        self.assertEqual(3, len(result.runs))
        self.assertTrue(all(r.branch_id == result.branch_id for r in result.runs))
        self.assertTrue(all(r.branch_id is not None for r in result.runs))
        task_after = self.store.get_task(task.id)
        self.assertEqual(TaskStatus.ACTIVE, task_after.status)

    def test_create_branches_respects_max_branches(self) -> None:
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-limit",
        )
        task = engine.create_task_with_policy(
            project_id=self.project.id,
            title="Limited branches",
            objective="Test max_branches cap",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            max_branches=2,
        )

        result = engine.create_branches(task.id, count=5)

        self.assertEqual(2, len(result.runs))

    def test_create_branches_rejects_single_branch_tasks(self) -> None:
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-single",
        )
        task = engine.create_task_with_policy(
            project_id=self.project.id,
            title="Single branch",
            objective="Should fail",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            max_branches=1,
        )

        with self.assertRaises(ValueError):
            engine.create_branches(task.id)

    def test_branch_events_are_recorded(self) -> None:
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-events",
        )
        task = engine.create_task_with_policy(
            project_id=self.project.id,
            title="Branch events",
            objective="Verify event audit trail",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            max_branches=2,
        )

        result = engine.create_branches(task.id)

        task_events = self.store.list_events("task", task.id)
        event_types = [e.event_type for e in task_events]
        self.assertIn("branch_started", event_types)
        self.assertIn("branches_completed", event_types)

        run_events = self.store.list_events("run", result.runs[0].id)
        run_event_types = [e.event_type for e in run_events]
        self.assertIn("branch_run_created", run_event_types)


class WinnerSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(base / "harness.db")
        self.store.initialize()
        self.project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(self.project)

    def test_select_winner_promotes_best_branch(self) -> None:
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace",
        )
        task = engine.create_task_with_policy(
            project_id=self.project.id,
            title="Winner selection",
            objective="Pick the best branch",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            max_branches=3,
        )

        branch_result = engine.create_branches(task.id)
        winner_result = engine.select_winner(task.id, branch_result.branch_id)

        self.assertIsNotNone(winner_result.winner_run)
        self.assertEqual("completed", winner_result.winner_run.status.value)
        task_after = self.store.get_task(task.id)
        self.assertEqual(TaskStatus.COMPLETED, task_after.status)

    def test_disposed_runs_are_marked(self) -> None:
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-dispose",
        )
        task = engine.create_task_with_policy(
            project_id=self.project.id,
            title="Disposal test",
            objective="Verify losing branches are disposed",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            max_branches=3,
        )

        branch_result = engine.create_branches(task.id)
        winner_result = engine.select_winner(task.id, branch_result.branch_id)

        self.assertEqual(2, len(winner_result.disposed_runs))
        for disposed in winner_result.disposed_runs:
            run = self.store.get_run(disposed.id)
            self.assertEqual("disposed", run.status.value)

    def test_winner_decision_is_recorded(self) -> None:
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-decision",
        )
        task = engine.create_task_with_policy(
            project_id=self.project.id,
            title="Decision test",
            objective="Verify winner gets a promote decision",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            max_branches=2,
        )

        branch_result = engine.create_branches(task.id)
        winner_result = engine.select_winner(task.id, branch_result.branch_id)

        decisions = self.store.list_decisions(winner_result.winner_run.id)
        self.assertTrue(any(d.action.value == "promote" for d in decisions))

    def test_winner_selection_events_are_recorded(self) -> None:
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-winner-events",
        )
        task = engine.create_task_with_policy(
            project_id=self.project.id,
            title="Winner events",
            objective="Verify audit trail for winner selection",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            max_branches=2,
        )

        branch_result = engine.create_branches(task.id)
        engine.select_winner(task.id, branch_result.branch_id)

        task_events = self.store.list_events("task", task.id)
        event_types = [e.event_type for e in task_events]
        self.assertIn("branch_winner_selected", event_types)

    def test_select_winner_fails_when_all_branches_failed(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-all-fail",
            worker=MissingArtifactWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project.id,
            title="All fail",
            objective="All branches produce incomplete artifacts",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            max_branches=2,
            required_artifacts=["plan", "report"],
        )

        branch_result = engine.create_branches(task.id)

        with self.assertRaises(ValueError):
            engine.select_winner(task.id, branch_result.branch_id)

        task_after = self.store.get_task(task.id)
        self.assertEqual(TaskStatus.FAILED, task_after.status)


class BranchDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(base / "harness.db")
        self.store.initialize()
        self.project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(self.project)

    def test_decider_returns_branch_when_retries_exhausted_and_branches_allowed(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-branch-decision",
            worker=MissingArtifactWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project.id,
            title="Branch on exhaustion",
            objective="Trigger branch decision after retry exhaustion",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            max_attempts=1,
            max_branches=2,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)
        decision = self.store.list_decisions(run.id)[0]

        self.assertEqual("branch", decision.action.value)
        task_after = self.store.get_task(task.id)
        self.assertEqual(TaskStatus.ACTIVE, task_after.status)

    def test_max_branches_round_trip(self) -> None:
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-roundtrip",
        )
        task = engine.create_task_with_policy(
            project_id=self.project.id,
            title="Roundtrip",
            objective="Verify max_branches persists",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            max_branches=5,
        )

        stored = self.store.get_task(task.id)
        self.assertEqual(5, stored.max_branches)
