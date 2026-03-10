from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import Project, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.interrogation import HarnessQueryService
from accruvia_harness.store import SQLiteHarnessStore


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
