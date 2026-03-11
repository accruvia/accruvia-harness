from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import Project, TaskStatus, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.gitlab import GitLabCLI
from accruvia_harness.services.issue_policy import IssueStatePolicy
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
                    "labels": ["bug", "runner"],
                    "milestone": {"title": "MVP"},
                    "assignees": [{"username": "sanaani"}],
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
                        "labels": ["bug", "runner"],
                        "milestone": {"title": "MVP"},
                        "assignees": [{"username": "sanaani"}],
                    },
                    {
                        "iid": 457,
                        "title": "Promote candidate",
                        "description": "Tighten promotion policy",
                        "state": "opened",
                        "web_url": "https://gitlab.com/soverton/accruvia/-/issues/457",
                        "labels": ["promotion"],
                        "milestone": None,
                        "assignees": [],
                    },
                ]
            )
        if args[:3] == ["glab", "api", "projects/soverton%2Faccruvia/issues/457"]:
            return json.dumps(
                {
                    "iid": 457,
                    "title": "Promote candidate",
                    "description": "Tighten promotion policy",
                    "state": "closed",
                    "web_url": "https://gitlab.com/soverton/accruvia/-/issues/457",
                    "labels": ["promotion"],
                    "milestone": None,
                    "assignees": [],
                }
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
        self.assertEqual(["bug", "runner"], task.external_ref_metadata["labels"])

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

    def test_report_task_to_gitlab_dedupes_identical_comment(self) -> None:
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
            close=True,
        )
        self.engine.report_task_to_gitlab(
            task_id=task.id,
            repo="soverton/accruvia",
            gitlab=self.gitlab,
            close=True,
        )

        note_calls = [call for call in self.runner.calls if call[:3] == ["glab", "issue", "note"]]
        self.assertEqual(1, len(note_calls))
        self.assertIn("Task: Fix runner", note_calls[0][-1])
        self.assertIn(
            ["glab", "issue", "close", "456", "--repo", "soverton/accruvia"],
            self.runner.calls,
        )

    def test_sync_gitlab_issue_state_reopens_when_task_not_completed(self) -> None:
        task = self.engine.import_gitlab_issue(
            project_id=self.project.id,
            repo="soverton/accruvia",
            issue=self.gitlab.fetch_issue("soverton/accruvia", "457"),
        )
        self.store.update_task_status(task.id, TaskStatus.PENDING)
        self.engine.sync_gitlab_issue_state(task.id, "soverton/accruvia", self.gitlab)
        self.assertIn(
            ["glab", "issue", "reopen", "457", "--repo", "soverton/accruvia"],
            self.runner.calls,
        )

    def test_sync_gitlab_issue_metadata_updates_task_metadata(self) -> None:
        task = self.engine.import_gitlab_issue(
            project_id=self.project.id,
            repo="soverton/accruvia",
            issue=self.gitlab.fetch_issue("soverton/accruvia", "456"),
        )
        updated = self.engine.sync_gitlab_issue_metadata(task.id, "soverton/accruvia", self.gitlab)
        self.assertEqual("MVP", updated.external_ref_metadata["milestone"])
        self.assertEqual(["sanaani"], updated.external_ref_metadata["assignees"])

    def test_completed_gitlab_task_can_stay_open_until_promotion_approved(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-policy",
            issue_state_policy=IssueStatePolicy(close_only_on_approved_promotion=True),
        )
        task = engine.import_gitlab_issue(
            project_id=self.project.id,
            repo="soverton/accruvia",
            issue=self.gitlab.fetch_issue("soverton/accruvia", "456"),
        )
        self.store.update_task_status(task.id, TaskStatus.COMPLETED)
        engine.sync_gitlab_issue_state(task.id, "soverton/accruvia", self.gitlab)
        close_calls = [call for call in self.runner.calls if call[:3] == ["glab", "issue", "close"]]
        self.assertEqual([], close_calls)
