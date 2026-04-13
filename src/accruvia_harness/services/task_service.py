from __future__ import annotations

from dataclasses import replace

from ..domain import (
    ContextRecord,
    Event,
    ObjectiveStatus,
    Project,
    PromotionMode,
    RepoProvider,
    Task,
    TaskStatus,
    WorkspacePolicy,
    new_id,
)
from ..store import SQLiteHarnessStore
from .common import task_created_payload

_REMEDIATION_TREE_CAP = 15
_RETRY_HISTORY_MAX_OBJECTIVE_CHARS = 280
_HIGH_RISK_TASK_STRATEGIES = {
    "objective_review_remediation",
    "atomic_from_mermaid",
    "operator_ergonomics",
    "sa_structural_fix",
    "sa_watch_direct_repair",
}


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
        # Auto-populate plan_id and mermaid_node_id so every new task joins
        # the objective -> plan -> task -> run lineage from the moment it
        # is created. This keeps the plans table and mermaid_node_id columns
        # consistent as the harness runs, matching the backfill shape for
        # existing tasks. See specs/atomic-plan-schema.md and
        # specs/plan-to-task-mapping.md for the intended lineage.
        task = self._ensure_plan_linkage(task)
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

    def _ensure_plan_linkage(self, task: Task) -> Task:
        """Attach a canonical Plan record and mermaid_node_id to a new task.

        Path 1 — task already has a plan_id: respect both values (idempotent).

        Path 2 — task has no plan_id: create a 1:1 plan row, then derive the
        task's mermaid_node_id from that plan via `canonical_node_id(plan)`.
        This guarantees task.mermaid_node_id == plan.mermaid_node_id == a
        stable P_<hash> that matches what `render_mermaid_from_plans` emits.

        The prior synthetic `T_<task-suffix>` fallback is removed: task IDs
        and plan IDs are no longer conflated. See `mermaid/render.py` and the
        Query #3 findings for the invariant this enforces.

        Tasks without an objective_id get no plan (plans are
        objective-scoped). Those are typically ad-hoc tasks not part of a
        decomposition.
        """
        from ..domain import Plan
        from ..mermaid import canonical_node_id

        if not task.objective_id:
            return task
        if task.plan_id and task.mermaid_node_id:
            return task

        plan_id = task.plan_id
        if not plan_id:
            plan = Plan(
                id=new_id("plan"),
                objective_id=task.objective_id,
                slice={
                    "derived_from": "task_service.create_task",
                    "task_id": task.id,
                    "task_title": task.title,
                    "label": task.title,
                    "files": list((task.scope or {}).get("files_to_touch") or []),
                },
                atomicity_assessment={
                    "is_atomic": True,
                    "violations": [],
                    "reason": "auto-created 1:1 plan",
                },
                approval_status="approved",
            )
            # Canonical mermaid_node_id derives from plan.id deterministically.
            plan.mermaid_node_id = canonical_node_id(plan)
            self.store.create_plan(plan)
            plan_id = plan.id
            node_id = plan.mermaid_node_id
        else:
            # Existing plan — look up its canonical id
            existing_plan = self.store.get_plan(plan_id) if hasattr(self.store, "get_plan") else None
            node_id = (
                existing_plan.mermaid_node_id if existing_plan is not None
                else task.mermaid_node_id
            )
        return replace(task, plan_id=plan_id, mermaid_node_id=node_id)

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
        mermaid_node_id: str | None = None,
    ) -> Task:
        if objective_id is not None:
            linked_objective = self.store.get_objective(objective_id)
            if linked_objective is None:
                raise ValueError(f"Unknown objective: {objective_id}")
            if linked_objective.project_id != project_id:
                raise ValueError(f"Objective {objective_id} does not belong to project {project_id}")
            # Copy non_negotiables from the objective's latest intent model into
            # task.scope so the self_review skill can enforce them on the diff.
            # Without this, the harness generates code that technically satisfies
            # the task title but violates explicit operator constraints (e.g.,
            # "test must be in file X, not file Y"). The contract is frozen at
            # task creation time so later intent revisions don't change the
            # contract for in-flight work.
            if scope is None or "non_negotiables" not in scope:
                intent = self.store.latest_intent_model(objective_id)
                if intent is not None and intent.non_negotiables:
                    scope = dict(scope or {})
                    scope["non_negotiables"] = [str(n).strip() for n in intent.non_negotiables if str(n).strip()]
        amended_objective, amended_metadata = self._apply_task_amendment(
            objective=objective,
            strategy=strategy,
            required_artifacts=required_artifacts or ["plan", "report"],
            validation_mode=str(validation_mode or "").strip() or "default_focused",
            scope=scope or {},
            external_ref_type=external_ref_type,
            external_ref_id=external_ref_id,
            external_ref_metadata=external_ref_metadata or {},
        )
        return self.create_task(
            Task(
                id=new_id("task"),
                project_id=project_id,
                objective_id=objective_id,
                mermaid_node_id=mermaid_node_id,
                title=title,
                objective=amended_objective,
                priority=priority,
                parent_task_id=parent_task_id,
                source_run_id=source_run_id,
                external_ref_type=external_ref_type,
                external_ref_id=external_ref_id,
                external_ref_metadata=amended_metadata,
                validation_profile=validation_profile,
                validation_mode=str(validation_mode or "").strip() or "default_focused",
                scope=scope or {},
                strategy=strategy,
                max_attempts=max_attempts,
                max_branches=max_branches,
                required_artifacts=required_artifacts or ["plan", "report"],
            )
        )

    def _apply_task_amendment(
        self,
        *,
        objective: str,
        strategy: str,
        required_artifacts: list[str],
        validation_mode: str,
        scope: dict[str, object],
        external_ref_type: str | None,
        external_ref_id: str | None,
        external_ref_metadata: dict[str, object],
    ) -> tuple[str, dict[str, object]]:
        metadata = dict(external_ref_metadata)
        if strategy not in _HIGH_RISK_TASK_STRATEGIES:
            return objective, metadata
        amendment_lines = [
            "Task Amendment:",
            "- Address repository code and test changes before relying on summaries or reports.",
            "- If no repository files change, the task is not complete.",
            f"- Required artifacts for completion: {', '.join(required_artifacts)}.",
            f"- Validation mode for this task: {validation_mode}.",
        ]
        if scope:
            amendment_lines.append("- Follow the task scope strictly when deciding which files to touch.")
        if strategy == "objective_review_remediation":
            amendment_lines.extend(
                [
                    "- This is a promotion-review remediation task. Artifact-shaped prose is not enough.",
                    "- The final evidence artifact must be backed by real repository state, real tests, or other durable run artifacts.",
                ]
            )
        if strategy in {"atomic_from_mermaid", "sa_structural_fix", "sa_watch_direct_repair", "operator_ergonomics"}:
            amendment_lines.append(
                "- This task is workflow-sensitive. Keep the change set narrow and avoid unrelated worker/control-plane modifications unless the objective explicitly requires them."
            )
        metadata["task_amendment"] = {
            "version": 1,
            "strategy": strategy,
            "required_artifacts": list(required_artifacts),
            "validation_mode": validation_mode,
            "scope_present": bool(scope),
            "external_ref_type": external_ref_type or "",
            "external_ref_id": external_ref_id or "",
            "lines": amendment_lines[1:],
        }
        return objective.rstrip() + "\n\n" + "\n".join(amendment_lines), metadata

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
        root_task = self._retry_tree_root(parent)
        blocked_reason, retry_history, current_failure = self._remediation_block_reason(
            parent=parent,
            root_task=root_task,
            source_run_id=source_run_id,
            findings=findings,
        )
        if blocked_reason:
            self._mark_retry_tree_blocked(
                task=parent,
                root_task=root_task,
                source_run_id=source_run_id,
                reason=blocked_reason,
                current_failure=current_failure,
            )
            return []
        created: list[Task] = []
        for index, finding in enumerate(findings, start=1):
            title = str(finding.get("title") or "").strip() or f"Remediate review finding {index}"
            base_objective = (
                str(finding.get("objective") or "").strip()
                or "Address the failed-task review finding and keep the work on the same objective."
            )
            objective = self._compose_remediation_objective(
                base_objective=base_objective,
                retry_history=retry_history,
            )
            metadata = {
                "failed_task_disposition": {
                    "kind": "split_into_narrower_tasks",
                    "source_task_id": parent_task_id,
                    "source_run_id": source_run_id,
                },
                "retry_remediation": {
                    "root_task_id": root_task.id,
                    "parent_task_id": parent_task_id,
                    "history": retry_history,
                    "current_failure": current_failure,
                    "history_summary": self._format_retry_history(retry_history),
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
            root_task = self._retry_tree_root(task)
            blocked_reason, _, current_failure = self._remediation_block_reason(
                parent=task,
                root_task=root_task,
                source_run_id=effective_run_id,
                findings=findings or [],
            )
            if blocked_reason:
                self._mark_retry_tree_blocked(
                    task=task,
                    root_task=root_task,
                    source_run_id=effective_run_id,
                    reason=blocked_reason,
                    current_failure=current_failure,
                )
                return {"status": "blocked", "task_id": task.id, "reason": blocked_reason}
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

    def _retry_tree_root(self, task: Task) -> Task:
        current = task
        visited = {task.id}
        while current.parent_task_id:
            parent = self.store.get_task(current.parent_task_id)
            if parent is None or parent.id in visited:
                break
            current = parent
            visited.add(parent.id)
        return current

    def _retry_tree_descendants(self, root_task: Task) -> list[Task]:
        tasks = self.store.list_tasks(root_task.project_id)
        children_by_parent: dict[str, list[Task]] = {}
        for candidate in tasks:
            if candidate.parent_task_id:
                children_by_parent.setdefault(candidate.parent_task_id, []).append(candidate)
        descendants: list[Task] = []
        stack = list(children_by_parent.get(root_task.id, []))
        seen: set[str] = set()
        while stack:
            candidate = stack.pop()
            if candidate.id in seen:
                continue
            seen.add(candidate.id)
            descendants.append(candidate)
            stack.extend(children_by_parent.get(candidate.id, []))
        descendants.sort(key=lambda item: item.created_at)
        return descendants

    def _remediation_block_reason(
        self,
        *,
        parent: Task,
        root_task: Task,
        source_run_id: str,
        findings: list[dict[str, object]],
    ) -> tuple[str | None, list[dict[str, object]], dict[str, object]]:
        descendants = self._retry_tree_descendants(root_task)
        if len(descendants) >= _REMEDIATION_TREE_CAP or len(descendants) + len(findings) > _REMEDIATION_TREE_CAP:
            return (
                f"Retry tree for root failure '{root_task.title}' reached the hard cap of {_REMEDIATION_TREE_CAP} remediation tasks.",
                self._build_retry_history(root_task, descendants),
                self._failure_classification(parent, source_run_id),
            )
        retry_history = self._build_retry_history(root_task, descendants)
        current_failure = self._failure_classification(parent, source_run_id)
        previous_failure = self._previous_retry_failure(retry_history, parent.id)
        if self._same_failure_classification(previous_failure, current_failure):
            return (
                "Latest remediation attempt repeated the previous root cause classification; escalating to blocked instead of spawning another retry.",
                retry_history,
                current_failure,
            )
        return None, retry_history, current_failure

    def _build_retry_history(self, root_task: Task, descendants: list[Task]) -> list[dict[str, object]]:
        history: list[dict[str, object]] = []
        ordered_tasks = [root_task, *descendants]
        for task in ordered_tasks:
            runs = self.store.list_runs(task.id)
            if not runs:
                continue
            run = runs[-1]
            evaluations = self.store.list_evaluations(run.id)
            decisions = self.store.list_decisions(run.id)
            evaluation = evaluations[-1] if evaluations else None
            decision = decisions[-1] if decisions else None
            history.append(
                {
                    "task_id": task.id,
                    "task_title": task.title,
                    "objective": task.objective,
                    "run_id": run.id,
                    "attempt": run.attempt,
                    "run_status": run.status.value,
                    "outcome": evaluation.summary if evaluation is not None else run.summary,
                    "verdict": evaluation.verdict.value if evaluation is not None else "",
                    "decision": decision.action.value if decision is not None else "",
                    "failure": self._failure_classification(task, run.id),
                }
            )
        return history

    def _failure_classification(self, task: Task, run_id: str) -> dict[str, object]:
        run = self.store.get_run(run_id)
        if run is None or run.task_id != task.id:
            runs = self.store.list_runs(task.id)
            run = runs[-1] if runs else None
        if run is None:
            return {"failure_category": "", "missing_required_artifacts": []}
        evaluations = self.store.list_evaluations(run.id)
        evaluation = evaluations[-1] if evaluations else None
        missing = []
        if evaluation is not None:
            raw_missing = evaluation.details.get("missing_required_artifacts")
            if isinstance(raw_missing, list):
                missing = sorted(str(item) for item in raw_missing if str(item).strip())
        failure_patterns = [item for item in self.store.list_failure_patterns(task_id=task.id) if item.run_id == run.id]
        category = failure_patterns[-1].category.value if failure_patterns else ""
        if not category and evaluation is not None:
            details = evaluation.details if isinstance(evaluation.details, dict) else {}
            diagnostics = details.get("diagnostics") if isinstance(details.get("diagnostics"), dict) else {}
            category = str(details.get("failure_category") or diagnostics.get("failure_category") or evaluation.verdict.value)
        return {
            "failure_category": category,
            "missing_required_artifacts": missing,
        }

    def _previous_retry_failure(self, retry_history: list[dict[str, object]], current_task_id: str) -> dict[str, object] | None:
        prior = [entry for entry in retry_history if str(entry.get("task_id") or "") != current_task_id]
        if not prior:
            return None
        latest = prior[-1].get("failure")
        return latest if isinstance(latest, dict) else None

    @staticmethod
    def _same_failure_classification(previous: dict[str, object] | None, current: dict[str, object]) -> bool:
        if not previous:
            return False
        previous_category = str(previous.get("failure_category") or "").strip()
        current_category = str(current.get("failure_category") or "").strip()
        previous_missing = sorted(str(item) for item in (previous.get("missing_required_artifacts") or []))
        current_missing = sorted(str(item) for item in (current.get("missing_required_artifacts") or []))
        return previous_category == current_category and previous_missing == current_missing

    def _compose_remediation_objective(self, *, base_objective: str, retry_history: list[dict[str, object]]) -> str:
        history_summary = self._format_retry_history(retry_history)
        if not history_summary:
            return base_objective
        return (
            f"{base_objective}\n\n"
            "Prior retry history for this root failure:\n"
            f"{history_summary}\n\n"
            f"This attempt must do something different from the prior rounds. Focus now on: {base_objective}"
        )

    def _format_retry_history(self, retry_history: list[dict[str, object]]) -> str:
        if not retry_history:
            return ""
        lines: list[str] = []
        for index, entry in enumerate(retry_history, start=1):
            objective = " ".join(str(entry.get("objective") or "").split())
            objective = objective[:_RETRY_HISTORY_MAX_OBJECTIVE_CHARS]
            failure = entry.get("failure") if isinstance(entry.get("failure"), dict) else {}
            category = str(failure.get("failure_category") or "unknown")
            missing = ", ".join(str(item) for item in (failure.get("missing_required_artifacts") or []))
            outcome = str(entry.get("outcome") or "").strip()
            outcome = " ".join(outcome.split())
            line = (
                f"{index}. {entry.get('task_title')} tried '{objective}'. "
                f"Outcome: {outcome or 'no evaluation summary recorded'} "
                f"(verdict={entry.get('verdict') or 'unknown'}, decision={entry.get('decision') or 'unknown'}, "
                f"failure_category={category}"
            )
            if missing:
                line += f", missing_artifacts={missing}"
            line += ")."
            lines.append(line)
        return "\n".join(lines)

    def _mark_retry_tree_blocked(
        self,
        *,
        task: Task,
        root_task: Task,
        source_run_id: str,
        reason: str,
        current_failure: dict[str, object],
    ) -> None:
        if task.objective_id:
            self.store.update_objective_status(task.objective_id, ObjectiveStatus.INVESTIGATING)
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="failed_task_retry_blocked",
                    project_id=task.project_id,
                    objective_id=task.objective_id,
                    task_id=task.id,
                    run_id=source_run_id or None,
                    visibility="operator_visible",
                    author_type="system",
                    content=reason,
                    metadata={
                        "root_task_id": root_task.id,
                        "retry_tree_descendants": len(self._retry_tree_descendants(root_task)),
                        "failure_classification": current_failure,
                    },
                )
            )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="failed_task_retry_blocked",
                payload={
                    "root_task_id": root_task.id,
                    "source_run_id": source_run_id,
                    "reason": reason,
                    "failure_classification": current_failure,
                },
            )
        )
