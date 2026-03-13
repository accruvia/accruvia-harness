from __future__ import annotations

import json
from pathlib import Path

from ..domain import (
    Artifact,
    Decision,
    DecisionAction,
    EvaluationVerdict,
    Event,
    Evaluation,
    Run,
    RunStatus,
    TaskStatus,
    new_id,
)
from ..policy import DefaultAnalyzer, DefaultDecider, DefaultPlanner, RetryStrategyAdvisor
from ..project_adapters import ProjectAdapterRegistry
from ..store import SQLiteHarnessStore
from ..workers import WorkerBackend
from .workspace_policy import WorkspacePolicyEnforcer


class RunService:
    def __init__(
        self,
        store: SQLiteHarnessStore,
        workspace_root: Path,
        planner: DefaultPlanner,
        worker: WorkerBackend,
        analyzer: DefaultAnalyzer,
        decider: DefaultDecider,
        project_adapter_registry: ProjectAdapterRegistry,
        task_service=None,
        retry_advisor: RetryStrategyAdvisor | None = None,
        telemetry=None,
        workspace_policy_enforcer: WorkspacePolicyEnforcer | None = None,
    ) -> None:
        self.store = store
        self.workspace_root = workspace_root
        self.planner = planner
        self.worker = worker
        self.analyzer = analyzer
        self.decider = decider
        self.project_adapter_registry = project_adapter_registry
        self.task_service = task_service
        self.retry_advisor = retry_advisor or RetryStrategyAdvisor()
        self.telemetry = telemetry
        self.workspace_policy_enforcer = workspace_policy_enforcer or WorkspacePolicyEnforcer()

    def run_once(self, task_id: str, progress_callback=None) -> Run:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise ValueError(f"Task {task_id} is already {task.status.value} — cannot run again")
        project = self.store.get_project(task.project_id)
        if project is None:
            raise ValueError(f"Unknown project for task: {task.project_id}")
        if self.telemetry is not None:
            with self.telemetry.timed(
                "run_cycle",
                task_id=task.id,
                project_id=task.project_id,
                validation_profile=task.validation_profile,
                strategy=task.strategy,
            ):
                return self._run_once(task, project, progress_callback=progress_callback)
        return self._run_once(task, project, progress_callback=progress_callback)

    def _run_once(self, task, project, progress_callback=None) -> Run:
        progress = progress_callback or (lambda _event: None)

        self.store.update_task_status(task.id, TaskStatus.ACTIVE)
        self.store.create_event(
            Event(id=new_id("event"), entity_type="task", entity_id=task.id, event_type="task_activated", payload={})
        )
        attempt = self.store.next_attempt(task.id)
        prior_runs = self.store.list_runs(task.id)
        previous_run = prior_runs[-1] if prior_runs else None
        previous_evaluations = self.store.list_evaluations(previous_run.id) if previous_run else []
        previous_decisions = self.store.list_decisions(previous_run.id) if previous_run else []
        retry_context = self.retry_advisor.advise(
            task=task,
            attempt=attempt,
            previous_run=previous_run,
            previous_evaluation=previous_evaluations[-1] if previous_evaluations else None,
            previous_decision=previous_decisions[-1].action if previous_decisions else None,
        )
        run = Run(
            id=new_id("run"),
            task_id=task.id,
            status=RunStatus.PLANNING,
            attempt=attempt,
            summary="Run created.",
        )
        self.store.create_run(run)
        progress(
            {
                "type": "run_created",
                "task_id": task.id,
                "task_title": task.title,
                "run_id": run.id,
                "attempt": attempt,
            }
        )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="run",
                entity_id=run.id,
                event_type="run_created",
                payload={"task_id": task.id, "attempt": attempt},
            )
        )
        if self.telemetry is not None:
            self.telemetry.metric(
                "run_started",
                1,
                task_id=task.id,
                run_id=run.id,
                attempt=attempt,
                validation_profile=task.validation_profile,
                strategy=task.strategy,
            )
        run_dir = self.workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        project_adapter = self.project_adapter_registry.get(project.adapter_name)
        prepared_workspace = project_adapter.prepare_workspace(project, task, run, run_dir)
        self.workspace_policy_enforcer.validate(project, prepared_workspace)
        for metadata_path in prepared_workspace.metadata_files:
            artifact = Artifact(
                id=new_id("artifact"),
                run_id=run.id,
                kind="workspace_metadata",
                path=str(metadata_path),
                summary="Prepared project workspace metadata",
            )
            self.store.create_artifact(artifact)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="artifact",
                    entity_id=artifact.id,
                    event_type="artifact_recorded",
                    payload={"run_id": run.id, "kind": artifact.kind, "path": artifact.path},
                )
            )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="run",
                entity_id=run.id,
                event_type="project_workspace_prepared",
                payload={
                    "project_id": project.id,
                    "project_adapter": project.adapter_name,
                    "project_root": str(prepared_workspace.project_root),
                    "workspace_mode": prepared_workspace.workspace_mode,
                    "source_repo_root": (
                        str(prepared_workspace.source_repo_root) if prepared_workspace.source_repo_root else None
                    ),
                    "branch_name": prepared_workspace.branch_name,
                    "diagnostics": prepared_workspace.diagnostics,
                },
            )
        )
        worker = self.worker
        build_worker = getattr(project_adapter, "build_worker", None)
        if callable(build_worker):
            override_worker = build_worker(project, task, run, prepared_workspace, self.worker)
            if override_worker is not None:
                worker = override_worker
                self.store.create_event(
                    Event(
                        id=new_id("event"),
                        entity_type="run",
                        entity_id=run.id,
                        event_type="project_worker_selected",
                        payload={
                            "project_adapter": project.adapter_name,
                            "worker_backend": type(worker).__name__,
                        },
                    )
                )

        if self.telemetry is not None:
            with self.telemetry.timed(
                "planning",
                task_id=task.id,
                run_id=run.id,
                attempt=attempt,
                validation_profile=task.validation_profile,
                retry=retry_context is not None,
            ):
                plan = self.planner.plan(task, retry_context)
        else:
            plan = self.planner.plan(task, retry_context)
        progress(
            {
                "type": "run_phase_changed",
                "task_id": task.id,
                "task_title": task.title,
                "run_id": run.id,
                "phase": "planning",
                "detail": plan.summary,
            }
        )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="run",
                entity_id=run.id,
                event_type="planned",
                payload={"summary": plan.summary, "retry_context": plan.retry_context or {}},
            )
        )
        if retry_context is not None:
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="run",
                    entity_id=run.id,
                    event_type="retry_strategy_selected",
                    payload=plan.retry_context or {},
                )
            )
        run = self.store.mark_run(run, RunStatus.WORKING, plan.summary)
        progress(
            {
                "type": "run_phase_changed",
                "task_id": task.id,
                "task_title": task.title,
                "run_id": run.id,
                "phase": "working",
                "detail": "Executing worker command and waiting for durable artifacts.",
            }
        )
        set_progress_callback = getattr(worker, "set_progress_callback", None)
        if callable(set_progress_callback):
            set_progress_callback(progress)
        try:
            if self.telemetry is not None:
                with self.telemetry.timed(
                    "work",
                    task_id=task.id,
                    run_id=run.id,
                    attempt=attempt,
                    validation_profile=task.validation_profile,
                    worker_backend=type(worker).__name__,
                ):
                    work = worker.work(task, run, self.workspace_root)
            else:
                work = worker.work(task, run, self.workspace_root)
        finally:
            if callable(set_progress_callback):
                set_progress_callback(None)
        work = self._ensure_failure_evidence(task, run, work)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="run",
                entity_id=run.id,
                event_type="worker_completed",
                payload={
                    "outcome": work.outcome,
                    "diagnostics": work.diagnostics or {},
                },
            )
        )
        for kind, path, summary in work.artifacts:
            artifact = Artifact(id=new_id("artifact"), run_id=run.id, kind=kind, path=path, summary=summary)
            self.store.create_artifact(artifact)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="artifact",
                    entity_id=artifact.id,
                    event_type="artifact_recorded",
                    payload={"run_id": run.id, "kind": kind, "path": path},
                )
            )
        run = self.store.mark_run(run, RunStatus.ANALYZING, work.summary)
        progress(
            {
                "type": "run_phase_changed",
                "task_id": task.id,
                "task_title": task.title,
                "run_id": run.id,
                "phase": "analyzing",
                "detail": work.summary,
            }
        )
        if self.telemetry is not None:
            self.telemetry.metric(
                "worker_result",
                1,
                task_id=task.id,
                run_id=run.id,
                outcome=work.outcome,
                validation_profile=task.validation_profile,
            )
            failure_category = str((work.diagnostics or {}).get("failure_category") or "").strip()
            if failure_category.endswith("_timeout"):
                self.telemetry.warn(
                    failure_category,
                    work.summary,
                    task_id=task.id,
                    run_id=run.id,
                    validation_profile=task.validation_profile,
                    worker_backend=type(worker).__name__,
                )
        if self.telemetry is not None:
            with self.telemetry.timed(
                "analyze",
                task_id=task.id,
                run_id=run.id,
                attempt=attempt,
                outcome=work.outcome,
            ):
                if work.outcome == "blocked":
                    analysis = self.analyzer.blocked(task, run, work.diagnostics)
                elif work.outcome == "failed":
                    analysis = self.analyzer.failed(task, run, work.diagnostics)
                else:
                    analysis = self.analyzer.analyze(task, run, self.store.list_artifacts(run.id))
        else:
            if work.outcome == "blocked":
                analysis = self.analyzer.blocked(task, run, work.diagnostics)
            elif work.outcome == "failed":
                analysis = self.analyzer.failed(task, run, work.diagnostics)
            else:
                analysis = self.analyzer.analyze(task, run, self.store.list_artifacts(run.id))
        evaluation = Evaluation(
            id=new_id("evaluation"),
            run_id=run.id,
            verdict=analysis.verdict,
            confidence=analysis.confidence,
            summary=analysis.summary,
            details=analysis.details,
        )
        self.store.create_evaluation(evaluation)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="evaluation",
                entity_id=evaluation.id,
                event_type="evaluation_recorded",
                payload={"run_id": run.id, "verdict": analysis.verdict, "confidence": analysis.confidence},
            )
        )
        run = self.store.mark_run(run, RunStatus.DECIDING, analysis.summary)
        progress(
            {
                "type": "run_phase_changed",
                "task_id": task.id,
                "task_title": task.title,
                "run_id": run.id,
                "phase": "deciding",
                "detail": analysis.summary,
            }
        )
        if self.telemetry is not None:
            self.telemetry.metric(
                "evaluation_recorded",
                1,
                task_id=task.id,
                run_id=run.id,
                verdict=analysis.verdict,
                validation_profile=task.validation_profile,
            )
        if self.telemetry is not None:
            with self.telemetry.timed(
                "decide",
                task_id=task.id,
                run_id=run.id,
                attempt=attempt,
                verdict=analysis.verdict,
            ):
                decision_result = self.decider.decide(analysis, run, task)
        else:
            decision_result = self.decider.decide(analysis, run, task)
        decision = Decision(
            id=new_id("decision"),
            run_id=run.id,
            action=decision_result.action,
            rationale=decision_result.rationale,
        )
        self.store.create_decision(decision)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="decision",
                entity_id=decision.id,
                event_type="decision_recorded",
                payload={"run_id": run.id, "action": decision.action.value},
            )
        )
        if bool(analysis.details.get("infrastructure_failure")):
            self._create_infrastructure_failure_follow_on(task, run, analysis)
        if analysis.verdict == EvaluationVerdict.BLOCKED:
            self._reshape_scope_violation(task, run, analysis)

        final_status = RunStatus.COMPLETED if decision_result.action == DecisionAction.PROMOTE else RunStatus.FAILED
        if analysis.verdict == EvaluationVerdict.BLOCKED:
            final_status = RunStatus.BLOCKED
        if decision_result.action == DecisionAction.BRANCH:
            final_status = RunStatus.FAILED
        task_status = TaskStatus.COMPLETED if decision_result.action == DecisionAction.PROMOTE else TaskStatus.PENDING
        if decision_result.action == DecisionAction.FAIL:
            task_status = TaskStatus.FAILED
        if decision_result.action == DecisionAction.BRANCH:
            task_status = TaskStatus.ACTIVE
        run = self.store.mark_run(run, final_status, decision_result.rationale)
        self.store.update_task_status(task.id, task_status)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="task_status_changed",
                payload={"status": task_status.value, "run_id": run.id},
            )
        )
        if self.telemetry is not None:
            self.telemetry.metric(
                "run_finished",
                1,
                task_id=task.id,
                run_id=run.id,
                run_status=final_status.value,
                task_status=task_status.value,
                decision=decision_result.action.value,
                validation_profile=task.validation_profile,
            )
            if decision_result.action == DecisionAction.RETRY:
                self.telemetry.metric(
                    "retry_selected",
                    1,
                    task_id=task.id,
                    run_id=run.id,
                    attempt=attempt,
                    validation_profile=task.validation_profile,
                )
            self.telemetry.metric(
                "run_attempt",
                attempt,
                metric_type="histogram",
                task_id=task.id,
                run_id=run.id,
                validation_profile=task.validation_profile,
            )
        return run

    def _ensure_failure_evidence(self, task, run, work):
        if work.outcome not in {"blocked", "failed"}:
            return work
        artifact_kinds = {kind for kind, _, _ in work.artifacts}
        if "report" in artifact_kinds:
            return work
        run_dir = self.workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        diagnostics = dict(work.diagnostics or {})
        failure_message = (
            diagnostics.get("failure_message")
            or diagnostics.get("error")
            or diagnostics.get("blocked_reason")
            or work.summary
        )
        report_path = run_dir / "report.json"
        report_path.write_text(
            json.dumps(
                {
                    "task_id": task.id,
                    "run_id": run.id,
                    "attempt": run.attempt,
                    "strategy": task.strategy,
                    "objective": task.objective,
                    "worker_outcome": work.outcome,
                    "infrastructure_failure": bool(diagnostics.get("infrastructure_failure")),
                    "failure_category": diagnostics.get("failure_category"),
                    "root_cause_hint": str(failure_message),
                    "diagnostics": diagnostics,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        work.artifacts.append(("report", str(report_path), "Structured failure evidence report"))
        return work

    def _create_infrastructure_failure_follow_on(self, task, run, analysis) -> None:
        if self.task_service is None:
            return
        if task.strategy == "executor_repair":
            return
        diagnostics = analysis.details.get("diagnostics")
        if not isinstance(diagnostics, dict):
            return
        if not bool(diagnostics.get("infrastructure_failure")):
            return
        existing = self.store.find_follow_on_task(task.id, run.id)
        if existing is not None and existing.strategy == "executor_repair":
            return
        queued_repairs = [
            child
            for child in self.store.list_child_tasks(task.id)
            if child.strategy == "executor_repair" and child.status in {TaskStatus.PENDING, TaskStatus.ACTIVE}
        ]
        if queued_repairs:
            return
        category = str(diagnostics.get("failure_category") or "executor_failure")
        message = str(
            diagnostics.get("failure_message")
            or diagnostics.get("error")
            or diagnostics.get("blocked_reason")
            or analysis.summary
        ).strip()
        follow_on = self.task_service.create_follow_on_task(
            parent_task_id=task.id,
            source_run_id=run.id,
            title=f"{task.title}: repair executor/runtime failure",
            objective=(
                "Repair the harness executor/runtime failure blocking task execution. "
                f"Latest category: {category}. Latest symptom: {message}"
            ),
            priority=max(task.priority + 100, 900),
            strategy="executor_repair",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=follow_on.id,
                event_type="executor_failure_follow_on_created",
                payload={"parent_task_id": task.id, "run_id": run.id, "failure_category": category},
            )
        )

    def _reshape_scope_violation(self, task, run, analysis) -> None:
        if self.task_service is None:
            return
        diagnostics = analysis.details.get("diagnostics")
        if not isinstance(diagnostics, dict):
            return
        scope_violation = diagnostics.get("scope_violation")
        if not isinstance(scope_violation, dict):
            return
        candidate_paths = scope_violation.get("outside_allowed_paths") or []
        if not isinstance(candidate_paths, list):
            return
        existing = self.store.list_child_tasks(task.id)
        existing_paths = {
            tuple(sorted((child.scope or {}).get("allowed_paths", [])))
            for child in existing
            if (child.scope or {}).get("allowed_paths")
        }
        created_paths: list[str] = []
        for path in [str(item) for item in candidate_paths if item]:
            key = (path,)
            if key in existing_paths:
                continue
            follow_on = self.task_service.create_task_with_policy(
                project_id=task.project_id,
                title=f"{task.title}: follow-up for {path}",
                objective=f"Apply the blocked out-of-scope change for `{path}` as a separate atomic task.",
                priority=max(task.priority - 10, 1),
                parent_task_id=task.id,
                source_run_id=run.id,
                external_ref_type=task.external_ref_type,
                external_ref_id=task.external_ref_id,
                external_ref_metadata=dict(task.external_ref_metadata),
                validation_profile=task.validation_profile,
                scope={"allowed_paths": [path]},
                strategy="scope_split",
                max_attempts=task.max_attempts,
                max_branches=task.max_branches,
                required_artifacts=list(task.required_artifacts),
            )
            existing_paths.add(key)
            created_paths.append(path)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=follow_on.id,
                    event_type="scope_split_task_created",
                    payload={"parent_task_id": task.id, "run_id": run.id, "path": path},
                )
            )
        if created_paths:
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="run",
                    entity_id=run.id,
                    event_type="scope_violation_reshaped",
                    payload={"created_paths": created_paths},
                )
            )

    def run_until_stable(self, task_id: str) -> list[Run]:
        completed_runs: list[Run] = []
        while True:
            task = self.store.get_task(task_id)
            if task is None:
                raise ValueError(f"Unknown task: {task_id}")
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                break
            run = self.run_once(task_id)
            completed_runs.append(run)
            decisions = self.store.list_decisions(run.id)
            latest_decision = decisions[-1] if decisions else None
            if latest_decision is not None and latest_decision.action == DecisionAction.BRANCH:
                branch_result = self._resolve_branching(task_id)
                completed_runs.extend(branch_result)
        return completed_runs

    def _resolve_branching(self, task_id: str) -> list[Run]:
        from .branch_service import BranchService

        branch_service = BranchService(
            store=self.store,
            workspace_root=self.workspace_root,
            planner=self.planner,
            worker=self.worker,
            analyzer=self.analyzer,
            project_adapter_registry=self.project_adapter_registry,
            telemetry=self.telemetry,
        )
        branch_result = branch_service.create_branches(task_id)
        try:
            branch_service.select_winner(task_id, branch_result.branch_id)
        except ValueError:
            pass
        return branch_result.runs
