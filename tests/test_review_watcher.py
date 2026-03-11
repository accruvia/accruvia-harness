from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from accruvia_harness.domain import Project, PromotionMode, PromotionRecord, PromotionStatus, RepoProvider, Run, RunStatus, Task, new_id
from accruvia_harness.services.review_watcher_service import ReviewWatcherService
from accruvia_harness.store import SQLiteHarnessStore


class _FakeGitHub:
    def __init__(self, state: str = "open", has_conflicts: bool = False) -> None:
        self.state = state
        self.has_conflicts = has_conflicts
        self.calls: list[tuple[str, str]] = []

    def fetch_pull_request_status(self, repo: str, head: str):
        self.calls.append((repo, head))
        return {
            "url": f"https://github.com/{repo}/pull/1",
            "state": self.state,
            "merge_state": "dirty" if self.has_conflicts else "clean",
            "has_conflicts": self.has_conflicts,
        }


class _FakeGitLab:
    def fetch_merge_request_status(self, repo: str, source_branch: str):
        return None


class ReviewWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(base / "harness.db")
        self.store.initialize()
        self.project = Project(
            id=new_id("project"),
            name="repo",
            description="repo",
            promotion_mode=PromotionMode.BRANCH_AND_PR,
            repo_provider=RepoProvider.GITHUB,
            repo_name="accruvia/routellect",
        )
        self.store.create_project(self.project)
        self.task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            title="Task",
            objective="Objective",
        )
        self.store.create_task(self.task)
        self.run = Run(id=new_id("run"), task_id=self.task.id, status=RunStatus.COMPLETED, attempt=1, summary="done")
        self.store.create_run(self.run)

    def _create_promotion(self, *, last_checked_at: str | None = None) -> PromotionRecord:
        details = {
            "applyback": {
                "status": "applied",
                "branch_name": "harness/task-1",
                "pr_url": "https://github.com/accruvia/routellect/pull/1",
            }
        }
        if last_checked_at is not None:
            details["review_watch"] = {"last_checked_at": last_checked_at, "state": "open", "has_conflicts": False}
        promotion = PromotionRecord(
            id=new_id("promotion"),
            task_id=self.task.id,
            run_id=self.run.id,
            status=PromotionStatus.APPROVED,
            summary="approved",
            details=details,
        )
        self.store.create_promotion(promotion)
        return promotion

    def test_check_due_reviews_updates_promotion_and_records_conflict(self) -> None:
        promotion = self._create_promotion()
        github = _FakeGitHub(state="open", has_conflicts=True)
        watcher = ReviewWatcherService(self.store, github=github, gitlab=_FakeGitLab())

        result = watcher.check_due_reviews(interval_seconds=28800, now=datetime(2026, 3, 11, tzinfo=UTC))

        self.assertEqual(1, result.checked_count)
        updated = self.store.list_promotions(self.task.id)[0]
        self.assertTrue(updated.details["review_watch"]["has_conflicts"])
        event_types = [event.event_type for event in self.store.list_events("task", self.task.id)]
        self.assertIn("promotion_review_synced", event_types)
        self.assertIn("promotion_merge_conflict_detected", event_types)
        self.assertEqual([("accruvia/routellect", "harness/task-1")], github.calls)

    def test_check_due_reviews_respects_interval(self) -> None:
        self._create_promotion(last_checked_at=datetime(2026, 3, 11, tzinfo=UTC).isoformat())
        github = _FakeGitHub()
        watcher = ReviewWatcherService(self.store, github=github, gitlab=_FakeGitLab())

        result = watcher.check_due_reviews(interval_seconds=28800, now=datetime(2026, 3, 11, 1, tzinfo=UTC))

        self.assertEqual(0, result.checked_count)
        self.assertEqual([], github.calls)
