from __future__ import annotations

from dataclasses import replace

from ..domain import Event, Project, PromotionMode, RepoProvider, Task, WorkspacePolicy, new_id
from ..store import SQLiteHarnessStore
from .common import task_created_payload


class TaskService:
    def __init__(self, store: SQLiteHarnessStore) -> None:
        self.store = store

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
    ) -> Project:
        project = Project(
            id=new_id("project"),
            name=name,
            description=description,
            adapter_name=adapter_name,
            workspace_policy=workspace_policy,
            promotion_mode=promotion_mode,
            repo_provider=repo_provider,
            repo_name=repo_name,
            base_branch=base_branch,
        )
        self.store.create_project(project)
        return project

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
    ) -> Project:
        existing = self.store.get_project(project_id)
        if existing is None:
            raise ValueError(f"Unknown project: {project_id}")
        project = replace(
            existing,
            name=name if name is not None else existing.name,
            description=description if description is not None else existing.description,
            adapter_name=adapter_name if adapter_name is not None else existing.adapter_name,
            workspace_policy=workspace_policy if workspace_policy is not None else existing.workspace_policy,
            promotion_mode=promotion_mode if promotion_mode is not None else existing.promotion_mode,
            repo_provider=repo_provider if repo_provider is not None else existing.repo_provider,
            repo_name=repo_name if repo_name is not None else existing.repo_name,
            base_branch=base_branch if base_branch is not None else existing.base_branch,
            max_concurrent_tasks=max_concurrent_tasks
            if max_concurrent_tasks is not None
            else existing.max_concurrent_tasks,
        )
        self.store.update_project(project)
        return project

    def create_task(self, task: Task) -> Task:
        if task.external_ref_type and task.external_ref_id and not task.parent_task_id and not task.source_run_id:
            existing = self.store.get_task_by_external_ref(task.external_ref_type, task.external_ref_id)
            if existing is not None:
                return existing
        self.store.create_task(task)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="task_created",
                payload=task_created_payload(task),
            )
        )
        return task

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
        external_ref_metadata: dict[str, object] | None = None,
        validation_profile: str = "generic",
        scope: dict[str, object] | None = None,
        strategy: str = "default",
        max_attempts: int = 3,
        max_branches: int = 1,
        required_artifacts: list[str] | None = None,
    ) -> Task:
        return self.create_task(
            Task(
                id=new_id("task"),
                project_id=project_id,
                title=title,
                objective=objective,
                priority=priority,
                parent_task_id=parent_task_id,
                source_run_id=source_run_id,
                external_ref_type=external_ref_type,
                external_ref_id=external_ref_id,
                external_ref_metadata=external_ref_metadata or {},
                validation_profile=validation_profile,
                scope=scope or {},
                strategy=strategy,
                max_attempts=max_attempts,
                max_branches=max_branches,
                required_artifacts=required_artifacts or ["plan", "report"],
            )
        )

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
    ) -> Task:
        parent = self.store.get_task(parent_task_id)
        if parent is None:
            raise ValueError(f"Unknown parent task: {parent_task_id}")
        task = self.create_task_with_policy(
            project_id=parent.project_id,
            title=title,
            objective=objective,
            priority=priority if priority is not None else parent.priority,
            parent_task_id=parent_task_id,
            source_run_id=source_run_id,
            external_ref_type=parent.external_ref_type,
            external_ref_id=parent.external_ref_id,
            external_ref_metadata=dict(parent.external_ref_metadata),
            validation_profile=parent.validation_profile,
            scope=dict(parent.scope),
            strategy=strategy or parent.strategy,
            max_attempts=max_attempts if max_attempts is not None else parent.max_attempts,
            required_artifacts=required_artifacts or list(parent.required_artifacts),
        )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="follow_on_task_created",
                payload={"parent_task_id": parent_task_id, "source_run_id": source_run_id},
            )
        )
        return task
