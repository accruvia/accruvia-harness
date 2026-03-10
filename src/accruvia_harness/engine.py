from __future__ import annotations

from pathlib import Path

from .github import GitHubCLI
from .gitlab import GitLabCLI
from .llm import LLMRouter
from .policy import DefaultAnalyzer, DefaultDecider, DefaultPlanner
from .services import (
    GitHubTaskService,
    GitLabTaskService,
    PromotionService,
    QueueService,
    RunService,
    TaskService,
)
from .store import SQLiteHarnessStore
from .workers import LocalArtifactWorker, WorkerBackend


class HarnessEngine:
    def __init__(
        self,
        store: SQLiteHarnessStore,
        workspace_root: str | Path,
        planner: DefaultPlanner | None = None,
        worker: WorkerBackend | None = None,
        analyzer: DefaultAnalyzer | None = None,
        decider: DefaultDecider | None = None,
        llm_router: LLMRouter | None = None,
    ) -> None:
        self.store = store
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.planner = planner or DefaultPlanner()
        self.worker = worker or LocalArtifactWorker()
        self.analyzer = analyzer or DefaultAnalyzer()
        self.decider = decider or DefaultDecider()
        self.llm_router = llm_router

        self.tasks = TaskService(self.store)
        self._build_services()

    def _build_services(self) -> None:
        self.runs = RunService(
            store=self.store,
            workspace_root=self.workspace_root,
            planner=self.planner,
            worker=self.worker,
            analyzer=self.analyzer,
            decider=self.decider,
        )
        self.queue = QueueService(self.store, self.runs)
        self.github_tasks = GitHubTaskService(self.tasks, self.store)
        self.gitlab_tasks = GitLabTaskService(self.tasks, self.store)
        self.promotions = PromotionService(self.store, self.tasks, self.workspace_root, llm_router=self.llm_router)

    def set_worker(self, worker: WorkerBackend) -> None:
        self.worker = worker
        self._build_services()

    def set_llm_router(self, llm_router: LLMRouter | None) -> None:
        self.llm_router = llm_router
        self._build_services()

    def create_project(self, name: str, description: str):
        return self.tasks.create_project(name, description)

    def create_task(self, project_id: str, title: str, objective: str):
        return self.tasks.create_task_with_policy(
            project_id=project_id,
            title=title,
            objective=objective,
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="generic",
            strategy="default",
            max_attempts=3,
            required_artifacts=["plan", "report"],
        )

    def create_task_with_policy(
        self,
        project_id: str,
        title: str,
        objective: str,
        priority: int,
        parent_task_id: str | None,
        source_run_id: str | None,
        external_ref_type: str | None,
        external_ref_id: str | None,
        validation_profile: str = "generic",
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ):
        return self.tasks.create_task_with_policy(
            project_id=project_id,
            title=title,
            objective=objective,
            priority=priority,
            parent_task_id=parent_task_id,
            source_run_id=source_run_id,
            external_ref_type=external_ref_type,
            external_ref_id=external_ref_id,
            validation_profile=validation_profile,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts or ["plan", "report"],
        )

    def run_once(self, task_id: str):
        return self.runs.run_once(task_id)

    def run_until_stable(self, task_id: str):
        return self.runs.run_until_stable(task_id)

    def process_next_task(
        self,
        project_id: str | None = None,
        worker_id: str = "local-worker",
        lease_seconds: int = 300,
    ):
        return self.queue.process_next_task(
            project_id=project_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    def process_queue(
        self,
        limit: int,
        project_id: str | None = None,
        worker_id: str = "local-worker",
        lease_seconds: int = 300,
    ):
        return self.queue.process_queue(
            limit=limit,
            project_id=project_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    def import_issue_task(
        self,
        project_id: str,
        issue_id: str,
        title: str,
        objective: str,
        priority: int = 100,
        validation_profile: str = "generic",
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ):
        return self.gitlab_tasks.import_issue_task(
            project_id=project_id,
            issue_id=issue_id,
            title=title,
            objective=objective,
            priority=priority,
            validation_profile=validation_profile,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts,
        )

    def import_gitlab_issue(
        self,
        project_id: str,
        repo: str,
        issue,
        priority: int = 100,
        validation_profile: str = "generic",
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ):
        return self.gitlab_tasks.import_gitlab_issue(
            project_id=project_id,
            issue=issue,
            priority=priority,
            validation_profile=validation_profile,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts,
        )

    def import_github_issue(
        self,
        project_id: str,
        repo: str,
        issue,
        priority: int = 100,
        validation_profile: str = "generic",
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ):
        return self.github_tasks.import_github_issue(
            project_id=project_id,
            issue=issue,
            priority=priority,
            validation_profile=validation_profile,
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
        validation_profile: str = "generic",
        strategy: str = "default",
        max_attempts: int = 3,
        required_artifacts: list[str] | None = None,
    ):
        return self.gitlab_tasks.sync_gitlab_open_issues(
            project_id=project_id,
            repo=repo,
            gitlab=gitlab,
            limit=limit,
            priority=priority,
            validation_profile=validation_profile,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts,
        )

    def report_task_to_gitlab(
        self,
        task_id: str,
        repo: str,
        gitlab: GitLabCLI,
        comment: str,
        close: bool = False,
    ):
        return self.gitlab_tasks.report_task_to_gitlab(
            task_id=task_id,
            repo=repo,
            gitlab=gitlab,
            comment=comment,
            close=close,
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
    ):
        return self.github_tasks.sync_github_open_issues(
            project_id=project_id,
            repo=repo,
            github=github,
            limit=limit,
            priority=priority,
            validation_profile=validation_profile,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts,
        )

    def report_task_to_github(
        self,
        task_id: str,
        repo: str,
        github: GitHubCLI,
        comment: str,
        close: bool = False,
    ):
        return self.github_tasks.report_task_to_github(
            task_id=task_id,
            repo=repo,
            github=github,
            comment=comment,
            close=close,
        )

    def sync_github_issue_state(
        self,
        task_id: str,
        repo: str,
        github: GitHubCLI,
    ):
        return self.github_tasks.sync_github_issue_state(task_id=task_id, repo=repo, github=github)

    def create_follow_on_task(
        self,
        parent_task_id: str,
        source_run_id: str,
        title: str,
        objective: str,
        priority: int | None = None,
        strategy: str | None = None,
        max_attempts: int | None = None,
        required_artifacts: list[str] | None = None,
    ):
        return self.tasks.create_follow_on_task(
            parent_task_id=parent_task_id,
            source_run_id=source_run_id,
            title=title,
            objective=objective,
            priority=priority,
            strategy=strategy,
            max_attempts=max_attempts,
            required_artifacts=required_artifacts,
        )

    def review_promotion(
        self,
        task_id: str,
        run_id: str | None = None,
        create_follow_on: bool = True,
    ):
        return self.promotions.review_task(
            task_id=task_id,
            run_id=run_id,
            create_follow_on=create_follow_on,
        )

    def affirm_promotion(
        self,
        task_id: str,
        run_id: str | None = None,
        promotion_id: str | None = None,
        create_follow_on: bool = True,
    ):
        return self.promotions.affirm_review(
            task_id=task_id,
            run_id=run_id,
            promotion_id=promotion_id,
            create_follow_on=create_follow_on,
        )
