from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..domain import (
    DecisionAction,
    Event,
    Run,
    RunStatus,
    TaskStatus,
    new_id,
)
from ..policy import AnalyzeResult, DefaultAnalyzer, DefaultPlanner, WorkResult
from ..project_adapters import ProjectAdapterRegistry
from ..store import SQLiteHarnessStore
from ..workers import WorkerBackend


@dataclass(slots=True)
class BranchResult:
    branch_id: str
    runs: list[Run]


@dataclass(slots=True)
class WinnerResult:
    winner_run: Run
    disposed_runs: list[Run]
    rationale: str


class BranchService:
    def __init__(
        self,
        store: SQLiteHarnessStore,
        workspace_root: Path,
        planner: DefaultPlanner,
        worker: WorkerBackend,
        analyzer: DefaultAnalyzer,
        project_adapter_registry: ProjectAdapterRegistry,
        telemetry=None,
    ) -> None:
        self.store = store
        self.workspace_root = workspace_root
        self.planner = planner
        self.worker = worker
        self.analyzer = analyzer
        self.project_adapter_registry = project_adapter_registry
        self.telemetry = telemetry

    def create_branches(self, task_id: str, count: int | None = None) -> BranchResult:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        project = self.store.get_project(task.project_id)
        if project is None:
            raise ValueError(f"Unknown project for task: {task.project_id}")

        branch_count = min(count or task.max_branches, task.max_branches)
        if branch_count < 2:
            raise ValueError(f"Branching requires max_branches >= 2, got {task.max_branches}")

        branch_id = new_id("branch")
        self.store.update_task_status(task.id, TaskStatus.ACTIVE)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="branch_started",
                payload={"branch_id": branch_id, "branch_count": branch_count},
            )
        )

        attempt = self.store.next_attempt(task.id)
        project_adapter = self.project_adapter_registry.get(project.adapter_name)
        runs: list[Run] = []

        for branch_index in range(branch_count):
            run = Run(
                id=new_id("run"),
                task_id=task.id,
                status=RunStatus.PLANNING,
                attempt=attempt + branch_index,
                summary=f"Speculative branch {branch_index + 1}/{branch_count}.",
                branch_id=branch_id,
            )
            self.store.create_run(run)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="run",
                    entity_id=run.id,
                    event_type="branch_run_created",
                    payload={
                        "task_id": task.id,
                        "branch_id": branch_id,
                        "branch_index": branch_index,
                        "branch_count": branch_count,
                    },
                )
            )

            run_dir = self.workspace_root / "runs" / run.id
            run_dir.mkdir(parents=True, exist_ok=True)
            project_adapter.prepare_workspace(project, task, run, run_dir)

            plan = self.planner.plan(task)
            run = self.store.mark_run(run, RunStatus.WORKING, plan.summary)

            work = self.worker.work(task, run, self.workspace_root)
            for kind, path, summary in work.artifacts:
                from ..domain import Artifact
                artifact = Artifact(id=new_id("artifact"), run_id=run.id, kind=kind, path=path, summary=summary)
                self.store.create_artifact(artifact)

            run = self.store.mark_run(run, RunStatus.ANALYZING, work.summary)

            if work.outcome == "blocked":
                analysis = self.analyzer.blocked(task, run, work.diagnostics)
            else:
                analysis = self.analyzer.analyze(task, run, self.store.list_artifacts(run.id))

            from ..domain import Evaluation
            evaluation = Evaluation(
                id=new_id("evaluation"),
                run_id=run.id,
                verdict=analysis.verdict,
                confidence=analysis.confidence,
                summary=analysis.summary,
                details=analysis.details,
            )
            self.store.create_evaluation(evaluation)

            final_status = RunStatus.COMPLETED if analysis.verdict == "acceptable" else RunStatus.FAILED
            run = self.store.mark_run(run, final_status, analysis.summary)
            runs.append(run)

        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="branches_completed",
                payload={
                    "branch_id": branch_id,
                    "run_ids": [r.id for r in runs],
                    "statuses": [r.status.value for r in runs],
                },
            )
        )

        return BranchResult(branch_id=branch_id, runs=runs)

    def select_winner(self, task_id: str, branch_id: str) -> WinnerResult:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")

        all_runs = self.store.list_runs(task_id)
        branch_runs = [r for r in all_runs if r.branch_id == branch_id]
        if not branch_runs:
            raise ValueError(f"No runs found for branch {branch_id}")

        completed_runs = [r for r in branch_runs if r.status == RunStatus.COMPLETED]
        if not completed_runs:
            self.store.update_task_status(task.id, TaskStatus.FAILED)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=task.id,
                    event_type="branch_selection_failed",
                    payload={
                        "branch_id": branch_id,
                        "reason": "No completed branch runs available.",
                    },
                )
            )
            raise ValueError(f"No completed runs in branch {branch_id}")

        scored: list[tuple[Run, float]] = []
        for run in completed_runs:
            evaluations = self.store.list_evaluations(run.id)
            if evaluations:
                best_confidence = max(e.confidence for e in evaluations)
                artifact_count = len(self.store.list_artifacts(run.id))
                score = best_confidence * 100 + artifact_count
            else:
                score = 0.0
            scored.append((run, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        winner = scored[0][0]
        winner_score = scored[0][1]

        disposed: list[Run] = []
        for run in branch_runs:
            if run.id == winner.id:
                continue
            self.store.mark_run(run, RunStatus.DISPOSED, f"Disposed in favor of winner {winner.id}.")
            disposed.append(run)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="run",
                    entity_id=run.id,
                    event_type="branch_run_disposed",
                    payload={
                        "branch_id": branch_id,
                        "winner_run_id": winner.id,
                    },
                )
            )

        from ..domain import Decision
        decision = Decision(
            id=new_id("decision"),
            run_id=winner.id,
            action=DecisionAction.PROMOTE,
            rationale=f"Selected as branch winner with score {winner_score:.1f} over {len(disposed)} other branch(es).",
        )
        self.store.create_decision(decision)

        self.store.update_task_status(task.id, TaskStatus.COMPLETED)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="branch_winner_selected",
                payload={
                    "branch_id": branch_id,
                    "winner_run_id": winner.id,
                    "winner_score": winner_score,
                    "disposed_run_ids": [r.id for r in disposed],
                },
            )
        )

        return WinnerResult(
            winner_run=winner,
            disposed_runs=disposed,
            rationale=decision.rationale,
        )
