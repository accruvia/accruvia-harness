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
                worker_backend=type(self.worker).__name__,
            ):
                work = self.worker.work(task, run, self.workspace_root)
        else:
            work = self.worker.work(task, run, self.workspace_root)
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
                else:
                    analysis = self.analyzer.analyze(task, run, self.store.list_artifacts(run.id))
        else:
            if work.outcome == "blocked":
                analysis = self.analyzer.blocked(task, run, work.diagnostics)
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

    def run_until_stable(self, task_id: str) -> list[Run]:
        completed_runs: list[Run] = []
        while True:
            task = self.store.get_task(task_id)
            if task is None:
                raise ValueError(f"Unknown task: {task_id}")
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                break
            completed_runs.append(self.run_once(task_id))
        return completed_runs
