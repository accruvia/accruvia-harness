from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import Project, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.gitlab import GitLabCLI
from accruvia_harness.store import SQLiteHarnessStore


class FakeGlabRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[:3] == ["glab", "api", "projects/soverton%2Faccruvia/issues/456"]:
            return json.dumps(
                {
                    "iid": 456,
                    "title": "Fix runner",
                    "description": "Repair missing artifact behavior",
                    "state": "opened",
                    "web_url": "https://gitlab.com/soverton/accruvia/-/issues/456",
                }
            )
        if args[:3] == ["glab", "api", "projects/soverton%2Faccruvia/issues?state=opened&per_page=2"]:
            return json.dumps(
                [
                    {
                        "iid": 456,
                        "title": "Fix runner",
                        "description": "Repair missing artifact behavior",
                        "state": "opened",
                        "web_url": "https://gitlab.com/soverton/accruvia/-/issues/456",
                    },
                    {
                        "iid": 457,
                        "title": "Promote candidate",
                        "description": "Tighten promotion policy",
                        "state": "opened",
                        "web_url": "https://gitlab.com/soverton/accruvia/-/issues/457",
                    },
                ]
            )
        return ""


class GitLabIntegrationTests(unittest.TestCase):
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
        self.runner = FakeGlabRunner()
        self.gitlab = GitLabCLI(runner=self.runner)

    def test_fetch_issue_and_import_gitlab_issue(self) -> None:
        issue = self.gitlab.fetch_issue("soverton/accruvia", "456")
        task = self.engine.import_gitlab_issue(
            project_id=self.project.id,
            repo="soverton/accruvia",
            issue=issue,
            priority=250,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )

        self.assertEqual("gitlab_issue", task.external_ref_type)
        self.assertEqual("456", task.external_ref_id)
        self.assertEqual("Fix runner", task.title)
        self.assertEqual("Repair missing artifact behavior", task.objective)

    def test_sync_gitlab_open_issues_is_idempotent(self) -> None:
        first = self.engine.sync_gitlab_open_issues(
            project_id=self.project.id,
            repo="soverton/accruvia",
            gitlab=self.gitlab,
            limit=2,
            priority=100,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        second = self.engine.sync_gitlab_open_issues(
            project_id=self.project.id,
            repo="soverton/accruvia",
            gitlab=self.gitlab,
            limit=2,
            priority=100,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )

        self.assertEqual(2, len(first))
        self.assertEqual(2, len(second))
        self.assertEqual(2, len(self.store.list_tasks()))

    def test_report_task_to_gitlab_posts_note_and_can_close(self) -> None:
        task = self.engine.import_issue_task(
            project_id=self.project.id,
            issue_id="456",
            title="Fix runner",
            objective="Repair missing artifact behavior",
            priority=200,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )

        self.engine.report_task_to_gitlab(
            task_id=task.id,
            repo="soverton/accruvia",
            gitlab=self.gitlab,
            comment="Harness completed the task.",
            close=True,
        )

        self.assertIn(
            ["glab", "issue", "note", "456", "--repo", "soverton/accruvia", "--message", "Harness completed the task."],
            self.runner.calls,
        )
        self.assertIn(
            ["glab", "issue", "close", "456", "--repo", "soverton/accruvia"],
            self.runner.calls,
        )
