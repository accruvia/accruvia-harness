from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from ..context_control import objective_execution_gate
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
        validation_service=None,
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
        self.validation_service = validation_service

    def run_once(self, task_id: str, progress_callback=None) -> Run:
        self.cleanup_stale_run_workspaces()
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task.objective_id:
            gate = objective_execution_gate(self.store, task.objective_id)
            if not gate.ready:
                blocking = next((item for item in gate.gate_checks if not item["ok"]), None)
                detail = str(blocking["detail"]) if blocking is not None else "Objective execution gate is not satisfied."
                raise ValueError(detail)
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

    def cleanup_stale_run_workspaces(self) -> dict[str, int]:
        runs_root = self.workspace_root / "runs"
        summary = {
            "removed": 0,
            "removed_orphaned": 0,
            "skipped_active": 0,
            "skipped_artifact_backed": 0,
            "skipped_unpromoted": 0,
            "missing_run": 0,
        }
        if not runs_root.exists():
            return summary
        terminal_statuses = {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.DISPOSED,
        }
        for run_root in runs_root.iterdir():
            workspace_dir = run_root / "workspace"
            if not run_root.is_dir() or not workspace_dir.exists():
                continue
            run = self.store.get_run(run_root.name)
            if run is None:
                summary["missing_run"] += 1
                self._remove_workspace_dir(run_root.name, workspace_dir)
                summary["removed_orphaned"] += 1
                continue
            if run.status not in terminal_statuses:
                summary["skipped_active"] += 1
                continue
            if self._workspace_contains_referenced_artifacts(run.id, workspace_dir):
                summary["skipped_artifact_backed"] += 1
                continue
            if run.status == RunStatus.COMPLETED and not self._run_is_promoted(run):
                summary["skipped_unpromoted"] += 1
                continue
            self._remove_workspace_dir(run.id, workspace_dir)
            summary["removed"] += 1
        return summary

    def _run_is_promoted(self, run) -> bool:
        """Check if this run's task has been promoted (code changes landed on main)."""
        task = self.store.get_task(run.task_id)
        if task is None:
            return False
        promotions = self.store.list_promotions(task.id)
        return any(p.status.value == "approved" for p in promotions)

    def _workspace_contains_referenced_artifacts(self, run_id: str, workspace_dir: Path) -> bool:
        workspace_resolved = workspace_dir.resolve()
        for artifact in self.store.list_artifacts(run_id):
            artifact_path = str(artifact.path or "").strip()
            if not artifact_path:
                continue
            candidate = Path(artifact_path).resolve(strict=False)
            if self._path_is_within(candidate, workspace_resolved):
                return True
        return False

    def _remove_workspace_dir(self, run_id: str, workspace_dir: Path) -> None:
        events = self.store.list_events(entity_type="run", entity_id=run_id)
        prepared_event = next(
            (event for event in reversed(events) if event.event_type == "project_workspace_prepared"),
            None,
        )
        source_repo_root = ""
        workspace_mode = ""
        if prepared_event is not None:
            source_repo_root = str(prepared_event.payload.get("source_repo_root") or "").strip()
            workspace_mode = str(prepared_event.payload.get("workspace_mode") or "").strip()
        if workspace_mode == "git_worktree" and source_repo_root:
            completed = subprocess.run(
                ["git", "worktree", "remove", "--force", str(workspace_dir)],
                cwd=source_repo_root,
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode == 0:
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=source_repo_root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                return
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir, ignore_errors=True)

    @staticmethod
    def _path_is_within(candidate: Path, parent: Path) -> bool:
        candidate_str = os.path.normcase(str(candidate))
        parent_str = os.path.normcase(str(parent))
        return candidate_str == parent_str or candidate_str.startswith(parent_str + os.sep)

    def _run_once(self, task, project, progress_callback=None) -> Run:
        progress = progress_callback or (lambda _event: None)

        self.store.update_task_status(task.id, TaskStatus.ACTIVE)
        try:
            return self._run_once_inner(task, project, progress)
        except Exception:
            # Guarantee: if _run_once_inner fails for any reason, the task
            # goes back to PENDING so it doesn't stay ACTIVE forever.
            current = self.store.get_task(task.id)
            if current is not None and current.status == TaskStatus.ACTIVE:
                self.store.update_task_status(task.id, TaskStatus.PENDING)
            raise

    def _run_once_inner(self, task, project, progress) -> Run:
        work, run, prepared_project_root = self._work_phase(task, project, progress)
        if run.status == RunStatus.BLOCKED:
            return run
        progress({"type": "ready_for_next", "task_id": task.id})
        return self._validation_phase(task, run, work, prepared_project_root, progress)

    def _work_phase(self, task, project, progress):
        """Everything up to and including worker.work(). Returns (WorkResult, Run, project_root)."""
        from ..policy import WorkResult as _WR

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
        _validating_announced = False

        def _phase_aware_progress(event):
            nonlocal _validating_announced
            if (
                not _validating_announced
                and isinstance(event, dict)
                and event.get("worker_phase") == "validating"
            ):
                _validating_announced = True
                self.store.mark_run(run, RunStatus.VALIDATING, "Compiling and running focused tests.")
            progress(event)

        set_progress_callback = getattr(worker, "set_progress_callback", None)
        if callable(set_progress_callback):
            set_progress_callback(_phase_aware_progress)
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

        # Credits exhausted: don't burn an attempt — requeue the task and signal the supervisor to freeze.
        if work.diagnostics and work.diagnostics.get("backends_unavailable"):
            run = self.store.mark_run(run, RunStatus.BLOCKED, "All LLM backends unavailable.")
            self.store.update_task_status(task.id, TaskStatus.PENDING)
            progress({
                "type": "backends_unavailable",
                "task_id": task.id,
                "run_id": run.id,
                "message": str(work.diagnostics.get("failure_message", "")),
            })
            return work, run, prepared_workspace.project_root

        return work, run, prepared_workspace.project_root

    def _validation_phase(self, task, run, work, project_workspace_root: Path, progress) -> Run:
        """Run validation on candidates, then analyze and decide."""
        attempt = run.attempt

        # If worker produced a candidate, run compile+test validation before analyzing.
        if work.outcome == "success" and work.diagnostics and work.diagnostics.get("worker_outcome") == "candidate":
            run = self.store.mark_run(run, RunStatus.VALIDATING, "Running compile and test validation.")
            progress({
                "type": "run_phase_changed",
                "task_id": task.id,
                "run_id": run.id,
                "phase": "validating",
            })
            if hasattr(self, "validation_service") and self.validation_service is not None:
                validation_result = self.validation_service.validate(task, run, work, project_workspace_root)
                # Update work result with validation outcome
                if validation_result is not None:
                    work = validation_result
            work = self._enforce_validation_evidence(task, run, work)

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
                    worker_backend="unknown",
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
        # Write scope narrowing / failure info into attempt_metadata instead of creating child tasks.
        self._record_attempt_metadata(task, run, analysis)
        # Scope violations are recorded in attempt_metadata (handled by _record_attempt_metadata above).

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
        # Emit structured failure diagnostic on any non-success outcome.
        if task_status != TaskStatus.COMPLETED:
            diagnostics = work.diagnostics or {}
            failure_report = {
                "task_id": task.id,
                "task_title": task.title,
                "run_id": run.id,
                "attempt": attempt,
                "max_attempts": task.max_attempts,
                "task_status": task_status.value,
                "run_status": final_status.value,
                "decision": decision_result.action.value,
                "decision_rationale": decision_result.rationale,
                "verdict": analysis.verdict.value if analysis.verdict else None,
                "analysis_summary": analysis.summary,
                "worker_outcome": work.outcome,
                "failure_category": str(diagnostics.get("failure_category") or ""),
                "failure_message": str(diagnostics.get("failure_message") or ""),
                "changed_files": diagnostics.get("changed_files", []),
                "atomicity_gate": diagnostics.get("atomicity_gate"),
                "compile_check": diagnostics.get("compile_check"),
                "test_check": diagnostics.get("test_check"),
                "validation_elapsed_seconds": diagnostics.get("validation_elapsed_seconds"),
                "infrastructure_failure": bool(diagnostics.get("infrastructure_failure")),
                "backends_unavailable": bool(diagnostics.get("backends_unavailable")),
            }
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="run",
                    entity_id=run.id,
                    event_type="failure_diagnostic",
                    payload=failure_report,
                )
            )
            progress({
                "type": "failure_diagnostic",
                **failure_report,
            })
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

    def _enforce_validation_evidence(self, task, run, work):
        diagnostics = dict(work.diagnostics or {})
        worker_outcome = str(diagnostics.get("worker_outcome") or "")
        compile_check = diagnostics.get("compile_check")
        test_check = diagnostics.get("test_check")
        missing_validation_evidence = worker_outcome == "candidate" or not isinstance(compile_check, dict) or not isinstance(test_check, dict)
        if not missing_validation_evidence:
            return work

        failure_message = (
            "Candidate validation did not persist deterministic compile_check and test_check evidence. "
            "The run cannot be promoted until validation writes those report fields."
        )
        diagnostics.update(
            {
                "worker_outcome": "failed",
                "failure_category": "validation_evidence_missing",
                "failure_message": failure_message,
                "compile_check": compile_check if isinstance(compile_check, dict) else None,
                "test_check": test_check if isinstance(test_check, dict) else None,
            }
        )
        run_dir = self.workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "report.json"
        if report_path.exists():
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
        else:
            payload = {}
        payload.update(
            {
                "task_id": task.id,
                "run_id": run.id,
                "worker_outcome": "failed",
                "failure_category": "validation_evidence_missing",
                "failure_message": failure_message,
            }
        )
        report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return type(work)(
            summary=failure_message,
            artifacts=list(work.artifacts),
            outcome="failed",
            diagnostics=diagnostics,
        )

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

    def _record_attempt_metadata(self, task, run, analysis) -> None:
        """Record scope narrowing and failure info into attempt_metadata instead of creating child tasks."""
        diagnostics = analysis.details.get("diagnostics")
        if not isinstance(diagnostics, dict):
            return
        metadata: dict[str, object] = {}

        # Record infrastructure failure info
        if bool(diagnostics.get("infrastructure_failure")) and not bool(diagnostics.get("backends_unavailable")):
            category = str(diagnostics.get("failure_category") or "executor_failure")
            message = str(
                diagnostics.get("failure_message")
                or diagnostics.get("error")
                or diagnostics.get("blocked_reason")
                or analysis.summary
            ).strip()
            metadata["infrastructure_failure"] = {
                "run_id": run.id,
                "category": category,
                "message": message,
            }

        # Record atomicity decomposition info
        category = str(diagnostics.get("failure_category") or "").strip()
        if category in {"atomicity_decomposition", "policy_self_modification"}:
            rationale = str(diagnostics.get("failure_message") or analysis.summary).strip()
            metadata["atomicity_narrowing"] = {
                "run_id": run.id,
                "category": category,
                "rationale": rationale,
            }

        # Record timeout decomposition info
        if category in {"validation_timeout", "stale_progress_timeout"} and task.strategy in {"executor_repair", "bounded_unblocker"}:
            timeout_seconds = diagnostics.get("timeout_seconds")
            metadata["timeout_narrowing"] = {
                "run_id": run.id,
                "category": category,
                "timeout_seconds": timeout_seconds,
            }

        if metadata:
            self.store.update_task_attempt_metadata(task.id, metadata)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=task.id,
                    event_type="attempt_metadata_recorded",
                    payload=metadata,
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
