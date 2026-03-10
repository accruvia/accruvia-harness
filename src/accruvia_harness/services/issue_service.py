from __future__ import annotations

from ..domain import Event, Task, TaskStatus, new_id
from ..issues import ExternalIssue, IssueProvider
from ..store import SQLiteHarnessStore
from .task_service import TaskService


class IssueTaskService:
    def __init__(self, task_service: TaskService, store: SQLiteHarnessStore, ref_type: str, event_prefix: str) -> None:
        self.task_service = task_service
        self.store = store
        self.ref_type = ref_type
        self.event_prefix = event_prefix

    def import_issue(
        self,
        project_id: str,
        issue: ExternalIssue,
        priority: int = 100,
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ) -> Task:
        objective = issue.body.strip() or issue.title
        return self.task_service.create_task_with_policy(
            project_id=project_id,
            title=issue.title,
            objective=objective,
            priority=priority,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=self.ref_type,
            external_ref_id=issue.issue_id,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts or ["plan", "report"],
        )

    def sync_open_issues(
        self,
        project_id: str,
        repo: str,
        provider: IssueProvider,
        limit: int,
        priority: int = 100,
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ) -> list[Task]:
        return [
            self.import_issue(
                project_id=project_id,
                issue=issue,
                priority=priority,
                strategy=strategy,
                max_attempts=max_attempts,
                required_artifacts=required_artifacts,
            )
            for issue in provider.list_open_issues(repo, limit)
        ]

    def report_task(
        self,
        task_id: str,
        repo: str,
        provider: IssueProvider,
        comment: str,
        close: bool = False,
        dedupe: bool = False,
    ) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task.external_ref_type != self.ref_type or not task.external_ref_id:
            raise ValueError(f"Task {task_id} is not linked to a {self.ref_type} issue")
        if dedupe and self._is_duplicate_report(task.id, comment, close):
            return task
        provider.add_comment(repo, task.external_ref_id, comment)
        if close:
            provider.close_issue(repo, task.external_ref_id)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type=f"{self.event_prefix}_reported",
                payload={
                    "repo": repo,
                    "close": close,
                    "external_ref_id": task.external_ref_id,
                    "comment": comment,
                },
            )
        )
        return task

    def sync_issue_state(self, task_id: str, repo: str, provider: IssueProvider) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task.external_ref_type != self.ref_type or not task.external_ref_id:
            raise ValueError(f"Task {task_id} is not linked to a {self.ref_type} issue")
        issue = provider.fetch_issue(repo, task.external_ref_id)
        desired = self._desired_issue_state(task.status)
        if desired == issue.state:
            return task
        if desired in {"closed", "close"}:
            provider.close_issue(repo, task.external_ref_id)
        else:
            provider.reopen_issue(repo, task.external_ref_id)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type=f"{self.event_prefix}_issue_state_synced",
                payload={
                    "repo": repo,
                    "external_ref_id": task.external_ref_id,
                    "from_state": issue.state,
                    "to_state": desired,
                },
            )
        )
        return task

    def _is_duplicate_report(self, task_id: str, comment: str, close: bool) -> bool:
        for event in self.store.list_events("task", task_id):
            if event.event_type != f"{self.event_prefix}_reported":
                continue
            if event.payload.get("comment") == comment and bool(event.payload.get("close")) == close:
                return True
        return False

    def _desired_issue_state(self, task_status: TaskStatus) -> str:
        if task_status == TaskStatus.COMPLETED:
            return "closed"
        return "open"
