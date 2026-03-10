from __future__ import annotations

from ..domain import Event, Task, new_id
from ..gitlab import GitLabCLI, GitLabIssue
from ..store import SQLiteHarnessStore
from .task_service import TaskService


class GitLabTaskService:
    def __init__(self, task_service: TaskService, store: SQLiteHarnessStore) -> None:
        self.task_service = task_service
        self.store = store

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
        return self.task_service.create_task_with_policy(
            project_id=project_id,
            title=title,
            objective=objective,
            priority=priority,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id=issue_id,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts or ["plan", "report"],
        )

    def import_gitlab_issue(
        self,
        project_id: str,
        issue: GitLabIssue,
        priority: int = 100,
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ) -> Task:
        objective = issue.description.strip() or issue.title
        return self.task_service.create_task_with_policy(
            project_id=project_id,
            title=issue.title,
            objective=objective,
            priority=priority,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id=issue.iid,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts or ["plan", "report"],
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
        return [
            self.import_gitlab_issue(
                project_id=project_id,
                issue=issue,
                priority=priority,
                strategy=strategy,
                max_attempts=max_attempts,
                required_artifacts=required_artifacts,
            )
            for issue in gitlab.list_open_issues(repo, limit)
        ]

    def report_task_to_gitlab(
        self,
        task_id: str,
        repo: str,
        gitlab: GitLabCLI,
        comment: str,
        close: bool = False,
    ) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task.external_ref_type != "gitlab_issue" or not task.external_ref_id:
            raise ValueError(f"Task {task_id} is not linked to a GitLab issue")
        gitlab.add_note(repo, task.external_ref_id, comment)
        if close:
            gitlab.close_issue(repo, task.external_ref_id)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="gitlab_reported",
                payload={"repo": repo, "close": close, "external_ref_id": task.external_ref_id},
            )
        )
        return task
