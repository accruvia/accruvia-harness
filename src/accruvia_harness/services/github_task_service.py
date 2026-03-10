from __future__ import annotations

from ..domain import Task
from ..github import GitHubCLI
from ..store import SQLiteHarnessStore
from .issue_service import IssueTaskService
from .task_service import TaskService


class GitHubTaskService(IssueTaskService):
    def __init__(self, task_service: TaskService, store: SQLiteHarnessStore) -> None:
        super().__init__(task_service=task_service, store=store, ref_type="github_issue", event_prefix="github")

    def import_github_issue(
        self,
        project_id: str,
        issue,
        priority: int = 100,
        validation_profile: str = "generic",
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ) -> Task:
        return self.import_issue(
            project_id=project_id,
            issue=issue,
            priority=priority,
            validation_profile=validation_profile,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts,
        )

    def sync_github_open_issues(
        self,
        project_id: str,
        repo: str,
        github: GitHubCLI,
        limit: int,
        priority: int = 100,
        validation_profile: str = "generic",
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ) -> list[Task]:
        return self.sync_open_issues(
            project_id, repo, github, limit, priority, validation_profile, strategy, max_attempts, required_artifacts
        )

    def report_task_to_github(
        self,
        task_id: str,
        repo: str,
        github: GitHubCLI,
        comment: str,
        close: bool = False,
    ) -> Task:
        return self.report_task(
            task_id=task_id,
            repo=repo,
            provider=github,
            comment=comment,
            close=close,
            dedupe=True,
        )

    def sync_github_issue_state(self, task_id: str, repo: str, github: GitHubCLI) -> Task:
        return self.sync_issue_state(task_id=task_id, repo=repo, provider=github)
