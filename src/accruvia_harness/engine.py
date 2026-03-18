from __future__ import annotations

from pathlib import Path

from .cognition import CognitionAdapterRegistry, build_cognition_registry
from .llm_availability import LLMAvailabilityGate
from .interrogation import HarnessQueryService
from .github import GitHubCLI
from .gitlab import GitLabCLI
from .llm import LLMRouter
from .policy import DefaultAnalyzer, DefaultDecider, DefaultPlanner
from .project_adapters import ProjectAdapterRegistry, build_project_adapter_registry
from .domain import PromotionMode, RepoProvider, WorkspacePolicy
from .services.issue_policy import IssueStatePolicy
from .services import (
    BranchService,
    CognitionService,
    GitHubTaskService,
    GitLabTaskService,
    PromotionService,
    QueueService,
    ReviewWatcherService,
    RunService,
    SupervisorService,
    TaskService,
)
from .services.validation_service import ValidationService
from .services.repository_promotion_service import RepositoryPromotionService
from .store import SQLiteHarnessStore
from .validation import PromotionValidatorRegistry, build_validator_registry
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
        project_adapter_registry: ProjectAdapterRegistry | None = None,
        validator_registry: PromotionValidatorRegistry | None = None,
        cognition_registry: CognitionAdapterRegistry | None = None,
        issue_state_policy: IssueStatePolicy | None = None,
        heartbeat_timeout_seconds: int = 1800,
        heartbeat_failure_escalation_threshold: int = 3,
        telemetry=None,
    ) -> None:
        self.store = store
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.planner = planner or DefaultPlanner()
        self.worker = worker or LocalArtifactWorker()
        self.analyzer = analyzer or DefaultAnalyzer()
        self.decider = decider or DefaultDecider()
        self.llm_router = llm_router
        self.project_adapter_registry = project_adapter_registry or build_project_adapter_registry()
        self.validator_registry = validator_registry or build_validator_registry()
        self.cognition_registry = cognition_registry or build_cognition_registry()
        self.issue_state_policy = issue_state_policy or IssueStatePolicy()
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.heartbeat_failure_escalation_threshold = heartbeat_failure_escalation_threshold
        self.telemetry = telemetry
        self.repository_promotions = RepositoryPromotionService()

        self.tasks = TaskService(self.store)
        self.query = HarnessQueryService(self.store, telemetry=self.telemetry)
        self._build_services()

    def _build_services(self) -> None:
        self.validation = ValidationService(
            store=self.store,
            workspace_root=self.workspace_root,
            telemetry=self.telemetry,
        )
        self.runs = RunService(
            store=self.store,
            workspace_root=self.workspace_root,
            planner=self.planner,
            worker=self.worker,
            analyzer=self.analyzer,
            decider=self.decider,
            project_adapter_registry=self.project_adapter_registry,
            task_service=self.tasks,
            telemetry=self.telemetry,
        )
        self.branches = BranchService(
            store=self.store,
            workspace_root=self.workspace_root,
            planner=self.planner,
            worker=self.worker,
            analyzer=self.analyzer,
            project_adapter_registry=self.project_adapter_registry,
            telemetry=self.telemetry,
        )
        self.llm_gate: LLMAvailabilityGate | None = None
        self.queue = QueueService(self.store, self.runs)
        self.github_tasks = GitHubTaskService(self.tasks, self.store, state_policy=self.issue_state_policy)
        self.gitlab_tasks = GitLabTaskService(self.tasks, self.store, state_policy=self.issue_state_policy)
        self.cognition = CognitionService(
            store=self.store,
            query_service=self.query,
            workspace_root=self.workspace_root,
            cognition_registry=self.cognition_registry,
            task_service=self.tasks,
            llm_router=self.llm_router,
            heartbeat_timeout_seconds=getattr(self, "heartbeat_timeout_seconds", 1800),
            telemetry=self.telemetry,
        )
        self.supervisor = SupervisorService(
            self.store,
            self.queue,
            self.cognition,
            heartbeat_failure_escalation_threshold=self.heartbeat_failure_escalation_threshold,
        )
        self.review_watcher = ReviewWatcherService(self.store, task_service=self.tasks)
        self.promotions = PromotionService(
            self.store,
            self.tasks,
            self.workspace_root,
            validator_registry=self.validator_registry,
            llm_router=self.llm_router,
            telemetry=self.telemetry,
            repository_promotions=self.repository_promotions,
        )

    def set_worker(self, worker: WorkerBackend) -> None:
        self.worker = worker
        self._build_services()

    def set_llm_router(self, llm_router: LLMRouter | None) -> None:
        self.llm_router = llm_router
        self._build_services()

    def set_llm_gate(self, gate: LLMAvailabilityGate) -> None:
        self.llm_gate = gate
        self.queue.llm_gate = gate

    def create_project(
        self,
        name: str,
        description: str,
        adapter_name: str = "generic",
        workspace_policy: WorkspacePolicy = WorkspacePolicy.ISOLATED_REQUIRED,
        promotion_mode: PromotionMode = PromotionMode.BRANCH_AND_PR,
        repo_provider: RepoProvider | None = None,
        repo_name: str | None = None,
        base_branch: str = "main",
    ):
        return self.tasks.create_project(
            name,
            description,
            adapter_name=adapter_name,
            workspace_policy=workspace_policy,
            promotion_mode=promotion_mode,
            repo_provider=repo_provider,
            repo_name=repo_name,
            base_branch=base_branch,
        )

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        adapter_name: str | None = None,
        workspace_policy: WorkspacePolicy | None = None,
        promotion_mode: PromotionMode | None = None,
        repo_provider: RepoProvider | None = None,
        repo_name: str | None = None,
        base_branch: str | None = None,
        max_concurrent_tasks: int | None = None,
    ):
        return self.tasks.update_project(
            project_id,
            name=name,
            description=description,
            adapter_name=adapter_name,
            workspace_policy=workspace_policy,
            promotion_mode=promotion_mode,
            repo_provider=repo_provider,
            repo_name=repo_name,
            base_branch=base_branch,
            max_concurrent_tasks=max_concurrent_tasks,
        )

    def create_task(self, project_id: str, title: str, objective: str):
        return self.tasks.create_task_with_policy(
            project_id=project_id,
            objective_id=None,
            title=title,
            objective=objective,
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="generic",
            validation_mode="default_focused",
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
        objective_id: str | None = None,
        validation_profile: str = "generic",
        validation_mode: str | None = None,
        scope: dict[str, object] | None = None,
        strategy: str = "default",
        max_attempts: int = 3,
        max_branches: int = 1,
        required_artifacts: list[str] | None = None,
    ):
        return self.tasks.create_task_with_policy(
            project_id=project_id,
            objective_id=objective_id,
            title=title,
            objective=objective,
            priority=priority,
            parent_task_id=parent_task_id,
            source_run_id=source_run_id,
            external_ref_type=external_ref_type,
            external_ref_id=external_ref_id,
            validation_profile=validation_profile,
            validation_mode=validation_mode,
            scope=scope,
            strategy=strategy,
            max_attempts=max_attempts,
            max_branches=max_branches,
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

    def heartbeat(self, project_id: str):
        return self.cognition.heartbeat(project_id)

    def supervise(
        self,
        project_id: str | None = None,
        worker_id: str = "supervisor",
        lease_seconds: int = 300,
        watch: bool = False,
        idle_sleep_seconds: float = 30.0,
        max_idle_cycles: int | None = 1,
        max_iterations: int | None = None,
        heartbeat_project_ids: list[str] | None = None,
        heartbeat_interval_seconds: float | None = None,
        heartbeat_all_projects: bool = False,
        review_check_enabled: bool = False,
        review_check_interval_seconds: int | None = None,
        stop_requested=None,
        progress_callback=None,
    ):
        return self.supervisor.run(
            project_id=project_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            watch=watch,
            idle_sleep_seconds=idle_sleep_seconds,
            max_idle_cycles=max_idle_cycles,
            max_iterations=max_iterations,
            heartbeat_project_ids=heartbeat_project_ids,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            heartbeat_all_projects=heartbeat_all_projects,
            review_check_enabled=review_check_enabled,
            review_check_interval_seconds=review_check_interval_seconds,
            review_watcher=self.review_watcher,
            stop_requested=stop_requested,
            progress_callback=progress_callback,
        )

    def check_reviews(self, interval_seconds: int):
        return self.review_watcher.check_due_reviews(interval_seconds)

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
        comment: str | None = None,
        close: bool | None = None,
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
        comment: str | None = None,
        close: bool | None = None,
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

    def sync_gitlab_issue_state(
        self,
        task_id: str,
        repo: str,
        gitlab: GitLabCLI,
    ):
        return self.gitlab_tasks.sync_gitlab_issue_state(task_id=task_id, repo=repo, gitlab=gitlab)

    def sync_github_issue_metadata(
        self,
        task_id: str,
        repo: str,
        github: GitHubCLI,
    ):
        return self.github_tasks.sync_github_issue_metadata(task_id=task_id, repo=repo, github=github)

    def sync_gitlab_issue_metadata(
        self,
        task_id: str,
        repo: str,
        gitlab: GitLabCLI,
    ):
        return self.gitlab_tasks.sync_gitlab_issue_metadata(task_id=task_id, repo=repo, gitlab=gitlab)

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

    def create_branches(self, task_id: str, count: int | None = None):
        return self.branches.create_branches(task_id, count=count)

    def select_winner(self, task_id: str, branch_id: str):
        return self.branches.select_winner(task_id, branch_id)

    def rereview_promotion(
        self,
        task_id: str,
        remediation_task_id: str,
        remediation_run_id: str | None = None,
        base_promotion_id: str | None = None,
        create_follow_on: bool = True,
    ):
        return self.promotions.rereview_task(
            task_id=task_id,
            remediation_task_id=remediation_task_id,
            remediation_run_id=remediation_run_id,
            base_promotion_id=base_promotion_id,
            create_follow_on=create_follow_on,
        )
