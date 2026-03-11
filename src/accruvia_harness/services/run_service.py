from __future__ import annotations

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

    def run_once(self, task_id: str) -> Run:
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
                return self._run_once(task, project)
        return self._run_once(task, project)

    def _run_once(self, task, project) -> Run:

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
        if self.telemetry is not None:
            self.telemetry.metric(
                "worker_result",
                1,
                task_id=task.id,
                run_id=run.id,
                outcome=work.outcome,
                validation_profile=task.validation_profile,
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
