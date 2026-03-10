from __future__ import annotations

from ..domain import Task
from ..gitlab import GitLabCLI
from ..store import SQLiteHarnessStore
from .issue_service import IssueTaskService
from .task_service import TaskService


class GitLabTaskService(IssueTaskService):
    def __init__(self, task_service: TaskService, store: SQLiteHarnessStore) -> None:
        super().__init__(task_service=task_service, store=store, ref_type="gitlab_issue", event_prefix="gitlab")

    def import_issue_task(
        self,
        project_id: str,
        issue_id: str,
        title: str,
        objective: str,
        priority: int = 100,
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ) -> Task:
        issue = self._direct_issue(issue_id, title, objective)
        return self.import_issue(
            project_id=project_id,
            issue=issue,
            priority=priority,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts,
        )

    def import_gitlab_issue(
        self,
        project_id: str,
        issue,
        priority: int = 100,
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ) -> Task:
        return self.import_issue(
            project_id=project_id,
            issue=issue,
            priority=priority,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts,
        )

    def sync_gitlab_open_issues(
        self,
        project_id: str,
        repo: str,
        gitlab: GitLabCLI,
        limit: int,
        priority: int = 100,
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ) -> list[Task]:
        return self.sync_open_issues(project_id, repo, gitlab, limit, priority, strategy, max_attempts, required_artifacts)

    def report_task_to_gitlab(
        self,
        task_id: str,
        repo: str,
        gitlab: GitLabCLI,
        comment: str,
        close: bool = False,
    ) -> Task:
        return self.report_task(
            task_id=task_id,
            repo=repo,
            provider=gitlab,
            comment=comment,
            close=close,
        )

    def _direct_issue(self, issue_id: str, title: str, objective: str):
        from .issue_service import ExternalIssue

        return ExternalIssue(issue_id=issue_id, title=title, body=objective, state="opened", url="")
