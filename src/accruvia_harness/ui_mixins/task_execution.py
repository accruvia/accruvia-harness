"""HarnessUIDataService task execution methods."""
from __future__ import annotations

import datetime as _dt
from typing import Any

from ..domain import (
    ContextRecord, Task, TaskStatus, new_id, serialize_dataclass,
)
from ._shared import _BACKGROUND_SUPERVISOR

class TaskExecutionMixin:

    def run_task(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        run = self.ctx.engine.run_once(task.id)
        return {"run": serialize_dataclass(run)}


    def retry_task(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task.status.value != "failed":
            raise ValueError(f"Task is {task.status.value}, not failed")
        self.store.update_task_status(task_id, TaskStatus.PENDING)
        engine = getattr(self.ctx, "engine", None)
        if engine is not None:
            _BACKGROUND_SUPERVISOR.start(task.project_id, engine, watch=True)
        return {"task_id": task_id, "status": "pending"}


    def apply_failed_task_disposition(
        self,
        task_id: str,
        *,
        disposition: str,
        rationale: str,
    ) -> dict[str, object]:
        result = self.task_service.apply_failed_task_disposition(
            task_id=task_id,
            disposition=disposition,
            rationale=rationale,
        )
        task = self.store.get_task(task_id)
        engine = getattr(self.ctx, "engine", None)
        if task is not None and engine is not None and disposition.strip().lower() in {"retry_as_is", "allow_manual_operator_implementation"}:
            _BACKGROUND_SUPERVISOR.start(task.project_id, engine, watch=True)
        return result


    def retry_all_failed(self, project_id: str) -> dict[str, object]:
        # Check LLM availability via the central gate before requeuing.
        gate = self.ctx.engine.llm_gate
        gate.reset()  # Force a fresh probe.
        if not gate.is_available():
            raise ValueError(f"No LLM backends available. Probes: {gate.last_probe_results}")

        tasks = self.store.list_tasks(project_id=project_id)
        reset_count = 0
        for task in tasks:
            if task.status == TaskStatus.FAILED:
                self.store.update_task_status(task.id, TaskStatus.PENDING)
                reset_count += 1
        engine = getattr(self.ctx, "engine", None)
        if reset_count > 0 and engine is not None:
            _BACKGROUND_SUPERVISOR.start(project_id, engine, watch=True)
        return {"reset_count": reset_count, "probe_results": gate.last_probe_results}


    def create_linked_task(self, objective_id: str) -> dict[str, object]:
        linked_objective = self.store.get_objective(objective_id)
        if linked_objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        proposal = self.proposed_first_task(objective_id)
        task = self.task_service.create_task_with_policy(
            project_id=linked_objective.project_id,
            objective_id=linked_objective.id,
            title=str(proposal["title"]),
            objective=str(proposal["objective"]),
            priority=linked_objective.priority,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="generic",
            validation_mode="lightweight_operator",
            scope={},
            strategy="operator_ergonomics",
            max_attempts=3,
            required_artifacts=["plan", "report"],
        )
        self.store.update_objective_phase(linked_objective.id)
        self.reconcile_objective_workflow(linked_objective.id)
        return {"task": serialize_dataclass(task)}


    def proposed_first_task(self, objective_id: str) -> dict[str, object]:
        linked_objective = self.store.get_objective(objective_id)
        if linked_objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        intent_model = self.store.latest_intent_model(objective_id)
        desired_outcome = (intent_model.intent_summary if intent_model is not None else "").strip()
        success_definition = (intent_model.success_definition if intent_model is not None else "").strip()
        summary = linked_objective.summary.strip()

        if desired_outcome:
            objective_text = desired_outcome
        elif summary:
            objective_text = summary
        else:
            objective_text = linked_objective.title

        if success_definition:
            objective_text = f"{objective_text} Success means: {success_definition}"

        return {
            "title": f"First slice: {linked_objective.title}",
            "objective": f"{objective_text} Keep the slice bounded and operator-visible.",
            "reason": "The harness generated this first slice from the objective, desired outcome, and success definition so you do not need to author the initial task manually.",
        }


    def _ensure_first_linked_task(self, objective_id: str) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        if any(task.objective_id == objective.id for task in self.store.list_tasks(objective.project_id)):
            return
        task_payload = self.create_linked_task(objective.id)
        task = task_payload["task"]
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="task_created",
                project_id=objective.project_id,
                objective_id=objective.id,
                task_id=str(task.get("id") or ""),
                visibility="model_visible",
                author_type="system",
                content=f"Created first bounded slice for objective {objective.title}",
                metadata={
                    "task_title": str(task.get("title") or ""),
                    "strategy": str(task.get("strategy") or ""),
                    "generated_from": "intent_and_mermaid",
                },
            )
        )


    def _auto_retry_restart_safe_failed_task(self, task: Task) -> bool:
        if task.status != TaskStatus.FAILED:
            return False
        runs = self.store.list_runs(task.id)
        if not runs:
            return False
        latest_run = runs[-1]
        metadata = dict(task.external_ref_metadata) if isinstance(task.external_ref_metadata, dict) else {}
        triage = metadata.get("auto_restart_triage") if isinstance(metadata.get("auto_restart_triage"), dict) else {}
        if str(triage.get("source_run_id") or "") == latest_run.id and task.status in {TaskStatus.PENDING, TaskStatus.ACTIVE}:
            return False

        reason = ""
        if latest_run.summary == "Recovered: process crash detected" and latest_run.attempt < task.max_attempts:
            reason = "recovered_process_crash"
        else:
            evaluations = self.store.list_evaluations(latest_run.id)
            latest_evaluation = evaluations[-1] if evaluations else None
            details = latest_evaluation.details if latest_evaluation is not None and isinstance(latest_evaluation.details, dict) else {}
            diagnostics = details.get("diagnostics") if isinstance(details.get("diagnostics"), dict) else {}
            failure_category = str(diagnostics.get("failure_category") or "").strip()
            infrastructure_failure = bool(diagnostics.get("infrastructure_failure"))
            restart_safe_categories = {"executor_process_failure", "executor_timeout", "llm_executor_failure", "workspace_contract_failure"}
            if infrastructure_failure and failure_category in restart_safe_categories and latest_run.attempt < task.max_attempts:
                reason = failure_category

        if not reason:
            return False

        metadata["auto_restart_triage"] = {
            "disposition": "retry_as_is",
            "reason": reason,
            "source_run_id": latest_run.id,
            "source_attempt": latest_run.attempt,
            "requeued_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        self.store.update_task_external_metadata(task.id, metadata)
        self.store.update_task_status(task.id, TaskStatus.PENDING)
        if task.objective_id:
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="action_receipt",
                    project_id=task.project_id,
                    objective_id=task.objective_id,
                    task_id=task.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Action receipt: Automatically requeued restart-safe failed task {task.title}.",
                    metadata={"kind": "failed_task_auto_requeued", "task_id": task.id, "source_run_id": latest_run.id, "reason": reason},
                )
            )
        engine = getattr(self.ctx, "engine", None)
        if engine is not None:
            _BACKGROUND_SUPERVISOR.start(task.project_id, engine, watch=True)
        return True

