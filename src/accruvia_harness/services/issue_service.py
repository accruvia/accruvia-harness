from __future__ import annotations

from dataclasses import dataclass

from ..domain import Event, Task, TaskStatus, new_id
from ..issues import ExternalIssue, IssueProvider
from ..store import SQLiteHarnessStore
from .issue_policy import IssueStatePolicy
from .task_service import TaskService


@dataclass(slots=True)
class IssueReportContext:
    comment: str
    close: bool
    issue_state: str


class IssueTaskService:
    def __init__(
        self,
        task_service: TaskService,
        store: SQLiteHarnessStore,
        ref_type: str,
        event_prefix: str,
        state_policy: IssueStatePolicy | None = None,
    ) -> None:
        self.task_service = task_service
        self.store = store
        self.ref_type = ref_type
        self.event_prefix = event_prefix
        self.state_policy = state_policy or IssueStatePolicy()

    def import_issue(
        self,
        project_id: str,
        issue: ExternalIssue,
        priority: int = 100,
        validation_profile: str = "generic",
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
            external_ref_metadata=issue.metadata(),
            validation_profile=validation_profile,
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
        validation_profile: str = "generic",
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ) -> list[Task]:
        tasks: list[Task] = []
        for issue in provider.list_open_issues(repo, limit):
            task = self.import_issue(
                project_id=project_id,
                issue=issue,
                priority=priority,
                validation_profile=validation_profile,
                strategy=strategy,
                max_attempts=max_attempts,
                required_artifacts=required_artifacts,
            )
            self.store.update_task_external_metadata(task.id, issue.metadata())
            tasks.append(self.store.get_task(task.id) or task)
        return tasks

    def report_task(
        self,
        task_id: str,
        repo: str,
        provider: IssueProvider,
        comment: str | None = None,
        close: bool | None = None,
        dedupe: bool = False,
    ) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task.external_ref_type != self.ref_type or not task.external_ref_id:
            raise ValueError(f"Task {task_id} is not linked to a {self.ref_type} issue")
        report = self._build_report_context(task, comment, close)
        if dedupe and self._is_duplicate_report(task.id, report.comment, report.close):
            return task
        provider.add_comment(repo, task.external_ref_id, report.comment)
        if report.close:
            provider.close_issue(repo, task.external_ref_id)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type=f"{self.event_prefix}_reported",
                payload={
                    "repo": repo,
                    "close": report.close,
                    "external_ref_id": task.external_ref_id,
                    "comment": report.comment,
                    "issue_state": report.issue_state,
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
        self.store.update_task_external_metadata(task.id, issue.metadata())
        desired = self.state_policy.desired_state(task.status, self._latest_promotion_status(task.id))
        normalized_current = "closed" if issue.state.lower() == "closed" else "open"
        if desired == "unchanged" or desired == normalized_current:
            return self.store.get_task(task.id) or task
        if desired == "closed":
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
        return self.store.get_task(task.id) or task

    def sync_issue_metadata(self, task_id: str, repo: str, provider: IssueProvider) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task.external_ref_type != self.ref_type or not task.external_ref_id:
            raise ValueError(f"Task {task_id} is not linked to a {self.ref_type} issue")
        issue = provider.fetch_issue(repo, task.external_ref_id)
        metadata = issue.metadata()
        self.store.update_task_external_metadata(task.id, metadata)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type=f"{self.event_prefix}_metadata_synced",
                payload={
                    "repo": repo,
                    "external_ref_id": task.external_ref_id,
                    "metadata": metadata,
                },
            )
        )
        return self.store.get_task(task.id) or task

    def _is_duplicate_report(self, task_id: str, comment: str, close: bool) -> bool:
        for event in self.store.list_events("task", task_id):
            if event.event_type != f"{self.event_prefix}_reported":
                continue
            if event.payload.get("comment") == comment and bool(event.payload.get("close")) == close:
                return True
        return False

    def _latest_promotion_status(self, task_id: str):
        promotion = self.store.latest_promotion(task_id)
        return promotion.status if promotion is not None else None

    def _build_report_context(
        self, task: Task, comment: str | None, close: bool | None
    ) -> IssueReportContext:
        latest_promotion = self.store.latest_promotion(task.id)
        desired_state = self.state_policy.desired_state(task.status, latest_promotion.status if latest_promotion else None)
        should_close = desired_state == "closed" if close is None else close
        return IssueReportContext(
            comment=comment or self._build_structured_comment(task, latest_promotion),
            close=should_close,
            issue_state=desired_state,
        )

    def _build_structured_comment(self, task: Task, latest_promotion) -> str:
        runs = self.store.list_runs(task.id)
        latest_run = runs[-1] if runs else None
        evaluations = self.store.list_evaluations(latest_run.id) if latest_run else []
        latest_evaluation = evaluations[-1] if evaluations else None
        decisions = self.store.list_decisions(latest_run.id) if latest_run else []
        latest_decision = decisions[-1] if decisions else None
        artifacts = self.store.list_artifacts(latest_run.id) if latest_run else []
        artifact_kinds = ", ".join(artifact.kind for artifact in artifacts) or "none"
        retry_count = max(0, len(runs) - 1)
        lines = [
            f"Task: {task.title}",
            f"Status: {task.status.value}",
            f"Runs: {len(runs)} ({retry_count} {'retry' if retry_count == 1 else 'retries'})",
        ]
        if latest_run is not None:
            lines.append(f"Latest Run: {latest_run.id} ({latest_run.status.value})")
        if latest_evaluation is not None:
            lines.append(f"Evaluation: {latest_evaluation.verdict.value} - {latest_evaluation.summary}")
        if latest_decision is not None:
            lines.append(f"Decision: {latest_decision.action.value} - {latest_decision.rationale}")
        if latest_promotion is not None:
            lines.append(f"Promotion: {latest_promotion.status.value} - {latest_promotion.summary}")
        lines.append(f"Artifacts: {artifact_kinds}")
        return "\n".join(lines)
