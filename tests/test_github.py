from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import Project, TaskStatus, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.github import GitHubCLI
from accruvia_harness.store import SQLiteHarnessStore


class FakeGhRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[:3] == ["gh", "api", "repos/accruvia/accruvia/issues/456"]:
            return json.dumps(
                {
                    "number": 456,
                    "title": "Fix runner",
                    "body": "Repair missing artifact behavior",
                    "state": "open",
                    "html_url": "https://github.com/accruvia/accruvia/issues/456",
                }
            )
        if args[:3] == ["gh", "api", "repos/accruvia/accruvia/issues?state=open&per_page=2"]:
            return json.dumps(
                [
                    {
                        "number": 456,
                        "title": "Fix runner",
                        "body": "Repair missing artifact behavior",
                        "state": "open",
                        "html_url": "https://github.com/accruvia/accruvia/issues/456",
                    },
                    {
                        "number": 457,
                        "title": "Promote candidate",
                        "body": "Tighten promotion policy",
                        "state": "open",
                        "html_url": "https://github.com/accruvia/accruvia/issues/457",
                    },
                ]
            )
        if args[:3] == ["gh", "api", "repos/accruvia/accruvia/issues/457"]:
            return json.dumps(
                {
                    "number": 457,
                    "title": "Promote candidate",
                    "body": "Tighten promotion policy",
                    "state": "closed",
                    "html_url": "https://github.com/accruvia/accruvia/issues/457",
                }
            )
        return ""


class GitHubIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(base / "harness.db")
        self.store.initialize()
        self.engine = HarnessEngine(store=self.store, workspace_root=base / "workspace")
        self.project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(self.project)
        self.runner = FakeGhRunner()
        self.github = GitHubCLI(runner=self.runner)

    def test_fetch_issue_and_import_github_issue(self) -> None:
        issue = self.github.fetch_issue("accruvia/accruvia", "456")
        task = self.engine.import_github_issue(
            project_id=self.project.id,
            repo="accruvia/accruvia",
            issue=issue,
            priority=250,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        self.assertEqual("github_issue", task.external_ref_type)
        self.assertEqual("456", task.external_ref_id)
        self.assertEqual("Fix runner", task.title)

    def test_sync_github_open_issues_is_idempotent(self) -> None:
        first = self.engine.sync_github_open_issues(
            project_id=self.project.id,
            repo="accruvia/accruvia",
            github=self.github,
            limit=2,
            priority=100,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        second = self.engine.sync_github_open_issues(
            project_id=self.project.id,
            repo="accruvia/accruvia",
            github=self.github,
            limit=2,
            priority=100,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        self.assertEqual(2, len(first))
        self.assertEqual(2, len(second))
        self.assertEqual(2, len(self.store.list_tasks()))

    def test_report_task_to_github_dedupes_identical_comment(self) -> None:
        task = self.engine.import_github_issue(
            project_id=self.project.id,
            repo="accruvia/accruvia",
            issue=self.github.fetch_issue("accruvia/accruvia", "456"),
        )
        self.engine.report_task_to_github(
            task_id=task.id,
            repo="accruvia/accruvia",
            github=self.github,
            comment="Harness completed the task.",
            close=True,
        )
        self.engine.report_task_to_github(
            task_id=task.id,
            repo="accruvia/accruvia",
            github=self.github,
            comment="Harness completed the task.",
            close=True,
        )
        comment_calls = [call for call in self.runner.calls if call[:3] == ["gh", "issue", "comment"]]
        self.assertEqual(1, len(comment_calls))

    def test_sync_github_issue_state_reopens_when_task_not_completed(self) -> None:
        task = self.engine.import_github_issue(
            project_id=self.project.id,
            repo="accruvia/accruvia",
            issue=self.github.fetch_issue("accruvia/accruvia", "457"),
        )
        self.store.update_task_status(task.id, TaskStatus.PENDING)
        self.engine.sync_github_issue_state(task.id, "accruvia/accruvia", self.github)
        self.assertIn(
            ["gh", "issue", "reopen", "457", "--repo", "accruvia/accruvia"],
            self.runner.calls,
        )
