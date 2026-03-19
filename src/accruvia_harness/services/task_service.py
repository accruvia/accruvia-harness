from __future__ import annotations

from dataclasses import replace

from ..domain import ContextRecord, Event, ObjectiveStatus, Project, PromotionMode, RepoProvider, Task, TaskStatus, WorkspacePolicy, new_id
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
        objective_id: str | None = None,
        external_ref_metadata: dict[str, object] | None = None,
        validation_profile: str = "generic",
        validation_mode: str | None = None,
        scope: dict[str, object] | None = None,
        strategy: str = "default",
        max_attempts: int = 3,
        max_branches: int = 1,
        required_artifacts: list[str] | None = None,
    ) -> Task:
        if objective_id is not None:
            linked_objective = self.store.get_objective(objective_id)
            if linked_objective is None:
                raise ValueError(f"Unknown objective: {objective_id}")
            if linked_objective.project_id != project_id:
                raise ValueError(f"Objective {objective_id} does not belong to project {project_id}")
        return self.create_task(
            Task(
                id=new_id("task"),
                project_id=project_id,
                objective_id=objective_id,
                title=title,
                objective=objective,
                priority=priority,
                parent_task_id=parent_task_id,
                source_run_id=source_run_id,
                external_ref_type=external_ref_type,
                external_ref_id=external_ref_id,
                external_ref_metadata=external_ref_metadata or {},
                validation_profile=validation_profile,
                validation_mode=str(validation_mode or "").strip() or "default_focused",
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
        validation_mode: str | None = None,
        max_attempts: int | None = None,
        required_artifacts: list[str] | None = None,
        external_ref_metadata_overrides: dict[str, object] | None = None,
    ) -> Task:
        parent = self.store.get_task(parent_task_id)
        if parent is None:
            raise ValueError(f"Unknown parent task: {parent_task_id}")
        merged_external_ref_metadata = dict(parent.external_ref_metadata)
        if external_ref_metadata_overrides:
            merged_external_ref_metadata.update(external_ref_metadata_overrides)
        task = self.create_task_with_policy(
            project_id=parent.project_id,
            objective_id=parent.objective_id,
            title=title,
            objective=objective,
            priority=priority if priority is not None else parent.priority,
            parent_task_id=parent_task_id,
            source_run_id=source_run_id,
            external_ref_type=parent.external_ref_type,
            external_ref_id=parent.external_ref_id,
            external_ref_metadata=merged_external_ref_metadata,
            validation_profile=parent.validation_profile,
            validation_mode=validation_mode if validation_mode is not None else parent.validation_mode,
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

    def create_tasks_from_review_findings(
        self,
        *,
        parent_task_id: str,
        source_run_id: str,
        findings: list[dict[str, object]],
    ) -> list[Task]:
        parent = self.store.get_task(parent_task_id)
        if parent is None:
            raise ValueError(f"Unknown parent task: {parent_task_id}")
        created: list[Task] = []
        for index, finding in enumerate(findings, start=1):
            title = str(finding.get("title") or "").strip() or f"Remediate review finding {index}"
            objective = str(finding.get("objective") or "").strip() or "Address the failed-task review finding and keep the work on the same objective."
            metadata = {
                "failed_task_disposition": {
                    "kind": "split_into_narrower_tasks",
                    "source_task_id": parent_task_id,
                    "source_run_id": source_run_id,
                },
                "promotion_remediation": {
                    "finding_ids": list(finding.get("finding_ids") or []),
                    "review_round_id": str(finding.get("review_round_id") or ""),
                    "dimension_name": str(finding.get("dimension_name") or ""),
                    "summary": str(finding.get("summary") or ""),
                    "remediation_hints": list(finding.get("remediation_hints") or []),
                },
            }
            follow_on = self.create_follow_on_task(
                parent_task_id=parent_task_id,
                source_run_id=source_run_id,
                title=title,
                objective=objective,
                strategy=str(finding.get("strategy") or parent.strategy or "atomic_from_mermaid"),
                validation_mode=str(finding.get("validation_mode") or parent.validation_mode or "default_focused"),
                external_ref_metadata_overrides=metadata,
            )
            created.append(follow_on)
        if parent.objective_id and created:
            self.store.update_objective_status(parent.objective_id, ObjectiveStatus.PLANNING)
        return created

    def apply_failed_task_disposition(
        self,
        *,
        task_id: str,
        disposition: str,
        rationale: str,
        source_run_id: str | None = None,
        findings: list[dict[str, object]] | None = None,
        operator_title: str | None = None,
        operator_objective: str | None = None,
        attempt_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        normalized = disposition.strip().lower()
        if task.status != TaskStatus.FAILED:
            raise ValueError(f"Task {task_id} must be failed before applying a failed-task disposition")
        runs = self.store.list_runs(task.id)
        effective_run_id = source_run_id or (runs[-1].id if runs else "")
        merged_metadata = dict(task.external_ref_metadata)
        disposition_payload = {
            "kind": normalized,
            "rationale": rationale.strip(),
            "source_run_id": effective_run_id,
        }
        merged_metadata["failed_task_disposition"] = disposition_payload

        if normalized == "retry_as_is":
            if attempt_metadata:
                self.store.update_task_attempt_metadata(task.id, attempt_metadata)
            self.store.update_task_external_metadata(task.id, merged_metadata)
            self.store.update_task_status(task.id, TaskStatus.PENDING)
            if task.objective_id:
                self.store.update_objective_status(task.objective_id, ObjectiveStatus.PLANNING)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=task.id,
                    event_type="failed_task_requeued",
                    payload=disposition_payload,
                )
            )
            return {"status": "pending", "task_id": task.id}

        if normalized == "split_into_narrower_tasks":
            created = self.create_tasks_from_review_findings(
                parent_task_id=task.id,
                source_run_id=effective_run_id,
                findings=findings or [],
            )
            self.store.update_task_external_metadata(task.id, merged_metadata)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=task.id,
                    event_type="failed_task_split",
                    payload={**disposition_payload, "created_task_ids": [item.id for item in created]},
                )
            )
            return {"status": "split", "task_ids": [item.id for item in created]}

        if normalized == "allow_manual_operator_implementation":
            follow_on = self.create_follow_on_task(
                parent_task_id=task.id,
                source_run_id=effective_run_id,
                title=operator_title or f"Manually implement: {task.title}",
                objective=operator_objective or task.objective,
                strategy="operator_ergonomics",
                validation_mode="lightweight_operator",
                external_ref_metadata_overrides={
                    "failed_task_disposition": disposition_payload,
                    "operator_owned": True,
                },
            )
            self.store.update_task_external_metadata(task.id, merged_metadata)
            if task.objective_id:
                self.store.update_objective_status(task.objective_id, ObjectiveStatus.PLANNING)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=task.id,
                    event_type="failed_task_manualized",
                    payload={**disposition_payload, "operator_task_id": follow_on.id},
                )
            )
            return {"status": "manual", "task_id": follow_on.id}

        if normalized == "waive_obsolete":
            self.store.update_task_external_metadata(task.id, merged_metadata)
            if task.objective_id:
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="failed_task_waived",
                        project_id=task.project_id,
                        objective_id=task.objective_id,
                        visibility="operator_visible",
                        author_type="system",
                        content=rationale.strip() or f"Waived failed task {task.title}.",
                        metadata=disposition_payload,
                    )
                )
                self.store.update_objective_phase(task.objective_id)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=task.id,
                    event_type="failed_task_waived",
                    payload=disposition_payload,
                )
            )
            return {"status": "waived", "task_id": task.id}

        raise ValueError(f"Unsupported failed-task disposition: {disposition}")
