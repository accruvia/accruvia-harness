from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import Project, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.interrogation import HarnessQueryService
from accruvia_harness.policy import WorkResult
from accruvia_harness.store import SQLiteHarnessStore
from accruvia_harness.workers import LocalArtifactWorker


class MissingArtifactWorker(LocalArtifactWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text("plan only\n", encoding="utf-8")
        return WorkResult(
            summary="Recorded partial artifacts.",
            artifacts=[("plan", str(plan_path), "Plan only")],
            outcome="failed",
            diagnostics={"reason": "missing_report"},
        )


class HarnessQueryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(base / "harness.db")
        self.store.initialize()
        self.engine = HarnessEngine(
            store=self.store,
            workspace_root=base / "workspace",
        )
        self.project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(self.project)
        self.query = HarnessQueryService(self.store)

    def test_portfolio_summary_reports_project_metrics(self) -> None:
        task = self.engine.import_issue_task(
            project_id=self.project.id,
            issue_id="500",
            title="Summary task",
            objective="Exercise summary output",
            priority=100,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.engine.run_until_stable(task.id)

        summary = self.query.portfolio_summary()

        self.assertEqual(1, summary["project_count"])
        self.assertEqual(1, summary["projects"][0]["metrics"]["tasks_by_status"]["completed"])

    def test_task_report_includes_runs_and_events(self) -> None:
        task = self.engine.import_issue_task(
            project_id=self.project.id,
            issue_id="501",
            title="Task report",
            objective="Exercise task report",
            priority=100,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.engine.run_until_stable(task.id)

        report = self.query.task_report(task.id)

        self.assertEqual(task.id, report["task"]["id"])
        self.assertEqual(1, len(report["runs"]))
        self.assertGreaterEqual(len(report["events"]), 3)

    def test_portfolio_summary_includes_retry_and_follow_on_metrics(self) -> None:
        failing_engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-retries",
            worker=MissingArtifactWorker(),
        )
        task = failing_engine.import_issue_task(
            project_id=self.project.id,
            issue_id="502",
            title="Retry task",
            objective="Cause retries",
            priority=100,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        runs = failing_engine.run_until_stable(task.id)
        failing_engine.create_follow_on_task(
            parent_task_id=task.id,
            source_run_id=runs[-1].id,
            title="Follow-on",
            objective="Handle failure",
        )

        summary = self.query.portfolio_summary()
        metrics = summary["global_metrics"]

        self.assertGreater(metrics["retry_rate"], 0.0)
        self.assertEqual(1, metrics["follow_on_task_count"])

    def test_operations_report_includes_profile_metrics_and_pending_affirmations(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project.id,
            title="Ops report",
            objective="Exercise ops report output",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="python",
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = self.engine.run_once(task.id)
        self.engine.review_promotion(task.id, run.id)

        report = self.query.operations_report(self.project.id)

        self.assertEqual(1, report["metrics"]["pending_promotions"])
        self.assertEqual(1, report["metrics"]["tasks_by_validation_profile"]["python"])
        self.assertEqual(1, len(report["pending_affirmations"]))

    def test_task_lineage_reports_ancestors_and_children(self) -> None:
        parent = self.engine.create_task_with_policy(
            project_id=self.project.id,
            title="Parent",
            objective="Parent task",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = self.engine.run_once(parent.id)
        child = self.engine.create_follow_on_task(
            parent_task_id=parent.id,
            source_run_id=run.id,
            title="Child",
            objective="Child task",
        )
        grandchild = self.engine.create_follow_on_task(
            parent_task_id=child.id,
            source_run_id=run.id,
            title="Grandchild",
            objective="Grandchild task",
        )

        lineage = self.query.task_lineage(child.id)

        self.assertEqual(parent.id, lineage["ancestors"][0]["id"])
        self.assertEqual(grandchild.id, lineage["children"][0]["task"]["id"])
