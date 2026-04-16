"""HarnessUIDataService workspace query methods."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ..commands.common import resolve_project_ref
from ..domain import (
    Objective, ObjectiveStatus, PromotionMode, RepoProvider,
    Task, serialize_dataclass,
)
from ._shared import _BACKGROUND_SUPERVISOR

from ._shared import _to_jsonable

from ..context_control import objective_execution_gate

class WorkspaceMixin:

    def project_workspace(self, project_ref: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objectives = self.store.list_objectives(project.id)
        for objective in objectives:
            self.reconcile_objective_workflow(objective.id)
        if self.auto_resume_atomic_generation:
            for objective in objectives:
                self._maybe_resume_atomic_generation(objective.id)
        if self.auto_resume_objective_review:
            for objective in objectives:
                self._maybe_resume_objective_review(objective.id)
        objectives = self.store.list_objectives(project.id)
        tasks = self.store.list_tasks(project.id)
        objective_task_map = {objective.id: [task for task in tasks if task.objective_id == objective.id] for objective in objectives}
        review_map: dict[str, dict[str, object]] = {}
        repo_promotion_map: dict[str, dict[str, object]] = {}
        workflow_map: dict[str, dict[str, object]] = {}
        for objective in objectives:
            linked_tasks = objective_task_map.get(objective.id, [])
            review_map[objective.id] = self._promotion_review_for_objective(objective.id, linked_tasks)
            repo_promotion_map[objective.id] = self._repo_promotion_for_objective(objective.id, linked_tasks)
            workflow_map[objective.id] = self._workflow_status_for_objective(
                objective,
                linked_tasks,
                review_map[objective.id],
                repo_promotion_map[objective.id],
            )
        task_payload = []
        latest_runs_by_task: dict[str, list[Any]] = {}
        for task in tasks:
            runs = self.store.list_runs(task.id)
            promotions = self.store.list_promotions(task.id)
            latest_runs_by_task[task.id] = runs
            review_ready = False
            if task.objective_id:
                review_ready = bool((workflow_map.get(task.objective_id) or {}).get("review", {}).get("ready"))
            task_payload.append(
                {
                    **serialize_dataclass(task),
                    "runs": [serialize_dataclass(run) for run in runs],
                    "promotions": [serialize_dataclass(promotion) for promotion in promotions],
                    "queue_state": self.workflow_service.queue_state_for_task(task, review_ready=review_ready),
                }
            )
        objective_payload = []
        for objective in objectives:
            latest_intent = self.store.latest_intent_model(objective.id)
            latest_mermaid = self.store.latest_mermaid_artifact(objective.id)
            latest_proposal = self._latest_mermaid_proposal(objective.id)
            gate = objective_execution_gate(self.store, objective.id)
            linked_tasks = objective_task_map.get(objective.id, [])
            atomic_generation = self._atomic_generation_state(objective.id)
            promotion_review = review_map[objective.id]
            repo_promotion = repo_promotion_map[objective.id]
            workflow = workflow_map[objective.id]
            objective_payload.append(
                {
                    **serialize_dataclass(objective),
                    "execution_gate": {
                        "ready": gate.ready,
                        "checks": _to_jsonable(gate.gate_checks),
                    },
                    "workflow": workflow,
                    "intent_model": serialize_dataclass(latest_intent) if latest_intent is not None else None,
                    "interrogation_review": self._interrogation_review(objective.id),
                    "diagram": (
                        {
                            **serialize_dataclass(latest_mermaid),
                            "content": latest_mermaid.content,
                        }
                        if latest_mermaid is not None
                        else None
                    ),
                    "diagram_proposal": latest_proposal,
                    "linked_task_count": len(linked_tasks),
                    "atomic_generation": atomic_generation,
                    "atomic_units": self._atomic_units_for_objective(objective.id, linked_tasks, atomic_generation),
                    "promotion_review": promotion_review,
                    "repo_promotion": repo_promotion,
                    "recommended_view": (
                        "promotion-review"
                        if workflow.get("review", {}).get("ready") or objective.status == ObjectiveStatus.RESOLVED
                        else "atomic"
                    ),
                    "proposed_first_task": self.proposed_first_task(objective.id)
                    if gate.ready and not linked_tasks
                    else None,
                }
            )
        return {
            "project": serialize_dataclass(project),
            "objectives": objective_payload,
            "tasks": task_payload,
            "comments": self._operator_comments(project.id),
            "replies": self._harness_replies(project.id),
            "action_receipts": self._action_receipts(project.id),
            "frustrations": self._operator_frustrations(project.id),
            "loop_status": self.query_service.project_summary(project.id)["loop_status"],
            "diagram": {
                "label": "Project control flow",
                "mermaid": self._project_mermaid(project.id, tasks, latest_runs_by_task),
            },
            "supervisor": {
                "running": _BACKGROUND_SUPERVISOR.is_running(project.id),
                **_BACKGROUND_SUPERVISOR.status(project.id),
            },
        }


    def project_summary_fast(self, project_ref: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objectives = self.store.list_objectives(project.id)
        tasks = self.store.list_tasks(project.id)
        task_counts_by_objective: dict[str, dict[str, int]] = {
            objective.id: {"completed": 0, "active": 0, "failed": 0, "pending": 0}
            for objective in objectives
        }
        for task in tasks:
            if not task.objective_id or task.objective_id not in task_counts_by_objective:
                continue
            status = task.status.value if hasattr(task.status, "value") else str(task.status)
            if status in task_counts_by_objective[task.objective_id]:
                task_counts_by_objective[task.objective_id][status] += 1
        objective_payload = [
            {
                "id": objective.id,
                "project_id": project.id,
                "title": objective.title,
                "status": objective.status.value,
                "task_counts": task_counts_by_objective.get(objective.id, {}),
                "task_total": sum(task_counts_by_objective.get(objective.id, {}).values()),
            }
            for objective in objectives
        ]
        objective_titles = {objective.id: objective.title for objective in objectives}
        task_payload = [
            {
                "id": task.id,
                "objective_id": task.objective_id,
                "objective_title": objective_titles.get(task.objective_id or "", ""),
                "title": task.title,
                "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "updated_at": task.updated_at.isoformat(),
            }
            for task in tasks
        ]
        return {
            "project": serialize_dataclass(project),
            "objectives": objective_payload,
            "tasks": task_payload,
            "supervisor": {
                "running": _BACKGROUND_SUPERVISOR.is_running(project.id),
                **_BACKGROUND_SUPERVISOR.status(project.id),
            },
        }


    def project_objectives_detail(self, project_ref: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objectives = self.store.list_objectives(project.id)
        tasks = self.store.list_tasks(project.id)
        objective_task_map = {objective.id: [task for task in tasks if task.objective_id == objective.id] for objective in objectives}
        payload = []
        for objective in objectives:
            linked_tasks = objective_task_map.get(objective.id, [])
            review = self._promotion_review_for_objective(objective.id, linked_tasks)
            workflow = self._harness_workflow_status_for_objective(objective, linked_tasks)
            gate = objective_execution_gate(self.store, objective.id)
            payload.append(
                {
                    "id": objective.id,
                    "project_id": project.id,
                    "title": objective.title,
                    "status": objective.status.value,
                    "execution_gate": {
                        "ready": gate.ready,
                        "checks": _to_jsonable(gate.gate_checks),
                    },
                    "workflow": workflow,
                    "promotion_review": {
                        "review_clear": bool(review.get("review_clear")),
                        "review_rounds": review.get("review_rounds") or [],
                    },
                }
            )
        return {
            "project": serialize_dataclass(project),
            "objectives": payload,
        }


    def project_objective_detail(self, project_ref: str, objective_id: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objective = self.store.get_objective(objective_id)
        if objective is None or objective.project_id != project.id:
            raise ValueError(f"Unknown objective for project: {objective_id}")
        current_interrogation = self._interrogation_review(objective.id)
        if not current_interrogation.get("completed") and self._should_auto_complete_interrogation(objective.id):
            self._persist_interrogation_record("interrogation_completed", objective, current_interrogation)
            self.reconcile_objective_workflow(objective.id)
        tasks = [task for task in self.store.list_tasks(project.id) if task.objective_id == objective.id]
        review = self._promotion_review_for_objective(objective.id, tasks)
        repo_promotion = self._repo_promotion_for_objective(objective.id, tasks)
        workflow = self._workflow_status_for_objective(objective, tasks, review, repo_promotion)
        gate = objective_execution_gate(self.store, objective.id)
        latest_intent = self.store.latest_intent_model(objective.id)
        latest_mermaid = self.store.latest_mermaid_artifact(objective.id)
        latest_proposal = self._latest_mermaid_proposal(objective.id)
        comment_records = self.store.list_context_records(objective_id=objective.id, record_type="operator_comment")[-12:]
        reply_records = self.store.list_context_records(objective_id=objective.id, record_type="harness_reply")[-12:]
        receipt_records = self.store.list_context_records(objective_id=objective.id, record_type="action_receipt")[-12:]
        task_payload = [
            {
                "id": task.id,
                "objective_id": task.objective_id,
                "title": task.title,
                "strategy": task.strategy,
                "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "updated_at": task.updated_at.isoformat(),
            }
            for task in tasks
        ]
        return {
            "project": serialize_dataclass(project),
            "objective": {
                **serialize_dataclass(objective),
                "execution_gate": {
                    "ready": gate.ready,
                    "checks": _to_jsonable(gate.gate_checks),
                },
                "workflow": workflow,
                "intent_model": serialize_dataclass(latest_intent) if latest_intent is not None else None,
                "interrogation_review": self._interrogation_review(objective.id),
                "diagram": (
                    {
                        **serialize_dataclass(latest_mermaid),
                        "content": latest_mermaid.content,
                    }
                    if latest_mermaid is not None
                    else None
                ),
                "diagram_proposal": latest_proposal,
                "promotion_review": review,
            },
            "tasks": task_payload,
            "comments": [
                {
                    "id": record.id,
                    "text": record.content,
                    "author": record.author_id,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id,
                    "task_id": record.task_id,
                }
                for record in comment_records
            ],
            "replies": [
                {
                    "id": record.id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id,
                    "task_id": record.task_id,
                    "reply_to": str(record.metadata.get("reply_to") or ""),
                }
                for record in reply_records
            ],
            "receipts": [
                {
                    "id": record.id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id,
                    "task_id": record.task_id,
                    "kind": str(record.metadata.get("kind") or ""),
                    "status": str(record.metadata.get("status") or ""),
                }
                for record in receipt_records
            ],
        }


    def project_token_performance(self, project_ref: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objectives = self.store.list_objectives(project.id)

        def summarize_packets(packet_list: list[dict[str, object]] | None) -> dict[str, float | int]:
            usage = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "latency_ms": 0,
                "reported_packet_count": 0,
                "unreported_packet_count": 0,
            }
            for packet in packet_list or []:
                llm_usage = packet.get("llm_usage") if isinstance(packet, dict) else {}
                llm_usage = llm_usage if isinstance(llm_usage, dict) else {}
                reported = packet.get("llm_usage_reported") is not False if isinstance(packet, dict) else True
                if reported:
                    usage["prompt_tokens"] += int(llm_usage.get("prompt_tokens") or 0)
                    usage["completion_tokens"] += int(llm_usage.get("completion_tokens") or 0)
                    usage["total_tokens"] += int(llm_usage.get("total_tokens") or 0)
                    usage["cost_usd"] += float(llm_usage.get("cost_usd") or 0.0)
                    usage["latency_ms"] += int(llm_usage.get("latency_ms") or 0)
                    usage["reported_packet_count"] += 1
                else:
                    usage["unreported_packet_count"] += 1
                    usage["latency_ms"] += int(llm_usage.get("latency_ms") or 0)
            return usage

        def add_usage(
            target: dict[str, float | int],
            usage: dict[str, float | int],
            *,
            packet_count: int = 0,
            round_count: int = 0,
        ) -> None:
            target["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            target["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            target["total_tokens"] += int(usage.get("total_tokens") or 0)
            target["cost_usd"] += float(usage.get("cost_usd") or 0.0)
            target["latency_ms"] += int(usage.get("latency_ms") or 0)
            target["packet_count"] += packet_count
            target["round_count"] += round_count
            target["reported_packet_count"] += int(usage.get("reported_packet_count") or 0)
            target["unreported_packet_count"] += int(usage.get("unreported_packet_count") or 0)

        totals: dict[str, float | int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": 0,
            "packet_count": 0,
            "round_count": 0,
            "reported_packet_count": 0,
            "unreported_packet_count": 0,
        }
        objective_rows: list[dict[str, object]] = []
        reviewer_rows: dict[str, dict[str, object]] = {}
        round_rows: list[dict[str, object]] = []

        for objective in objectives:
            linked_tasks = [task for task in self.store.list_tasks(project.id) if task.objective_id == objective.id]
            review = self._promotion_review_for_objective(objective.id, linked_tasks)
            rounds = list(review.get("review_rounds") or [])
            if not rounds:
                continue
            objective_usage: dict[str, float | int] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "latency_ms": 0,
                "packet_count": 0,
                "round_count": 0,
                "reported_packet_count": 0,
                "unreported_packet_count": 0,
            }
            for round_row in rounds:
                packets = list(round_row.get("packets") or [])
                round_usage = summarize_packets(packets)
                add_usage(objective_usage, round_usage, packet_count=len(packets), round_count=1)
                add_usage(totals, round_usage, packet_count=len(packets), round_count=1)
                round_rows.append(
                    {
                        "objective_id": objective.id,
                        "objective_title": objective.title,
                        "round_number": round_row.get("round_number"),
                        "status": round_row.get("status"),
                        "packet_count": len(packets),
                        "usage": round_usage,
                        "last_activity_at": round_row.get("last_activity_at"),
                    }
                )
                for packet in packets:
                    reviewer = str(packet.get("reviewer") or packet.get("dimension") or "unknown")
                    current = reviewer_rows.get(
                        reviewer,
                        {
                            "reviewer": reviewer,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                            "cost_usd": 0.0,
                            "latency_ms": 0,
                            "packet_count": 0,
                            "reported_packet_count": 0,
                            "unreported_packet_count": 0,
                        },
                    )
                    packet_usage = summarize_packets([packet])
                    current["prompt_tokens"] = int(current["prompt_tokens"]) + int(packet_usage["prompt_tokens"])
                    current["completion_tokens"] = int(current["completion_tokens"]) + int(packet_usage["completion_tokens"])
                    current["total_tokens"] = int(current["total_tokens"]) + int(packet_usage["total_tokens"])
                    current["cost_usd"] = float(current["cost_usd"]) + float(packet_usage["cost_usd"])
                    current["latency_ms"] = int(current["latency_ms"]) + int(packet_usage["latency_ms"])
                    current["packet_count"] = int(current["packet_count"]) + 1
                    current["reported_packet_count"] = int(current["reported_packet_count"]) + int(packet_usage["reported_packet_count"])
                    current["unreported_packet_count"] = int(current["unreported_packet_count"]) + int(packet_usage["unreported_packet_count"])
                    reviewer_rows[reviewer] = current
            objective_rows.append(
                {
                    "objective_id": objective.id,
                    "title": objective.title,
                    "round_count": int(objective_usage["round_count"]),
                    "packet_count": int(objective_usage["packet_count"]),
                    "usage": objective_usage,
                }
            )

        objective_rows.sort(key=lambda item: int((item.get("usage") or {}).get("total_tokens") or 0), reverse=True)
        round_rows.sort(key=lambda item: int((item.get("usage") or {}).get("total_tokens") or 0), reverse=True)
        reviewers = sorted(reviewer_rows.values(), key=lambda item: int(item.get("total_tokens") or 0), reverse=True)
        avg_tokens_per_round = int(int(totals["total_tokens"]) / int(totals["round_count"])) if int(totals["round_count"]) else 0
        avg_cost_per_round = float(totals["cost_usd"]) / int(totals["round_count"]) if int(totals["round_count"]) else 0.0
        avg_tokens_per_packet = int(int(totals["total_tokens"]) / int(totals["packet_count"])) if int(totals["packet_count"]) else 0

        return {
            "project": serialize_dataclass(project),
            "totals": totals,
            "summary": {
                "avg_tokens_per_round": avg_tokens_per_round,
                "avg_cost_per_round": avg_cost_per_round,
                "avg_tokens_per_packet": avg_tokens_per_packet,
            },
            "objectives": objective_rows,
            "reviewers": reviewers,
            "rounds": round_rows[:50],
        }


    def list_projects(self) -> dict[str, object]:
        projects = []
        for project in self.store.list_projects():
            metrics = self.store.metrics_snapshot(project.id)
            projects.append(
                {
                    **serialize_dataclass(project),
                    "queue_depth": int(metrics.get("tasks_by_status", {}).get("pending", 0))
                    + int(metrics.get("tasks_by_status", {}).get("active", 0)),
                }
            )
        return {"projects": projects}


    def update_project_repo_settings(
        self,
        project_id: str,
        *,
        promotion_mode: str,
        repo_provider: str,
        repo_name: str,
        base_branch: str,
    ) -> dict[str, object]:
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        cleaned_repo_name = repo_name.strip()
        cleaned_base_branch = base_branch.strip()
        if not cleaned_repo_name:
            raise ValueError("Repository name must not be empty")
        if not cleaned_base_branch:
            raise ValueError("Base branch must not be empty")
        updated = self.task_service.update_project(
            project.id,
            promotion_mode=PromotionMode(promotion_mode),
            repo_provider=RepoProvider(repo_provider),
            repo_name=cleaned_repo_name,
            base_branch=cleaned_base_branch,
        )
        return {"project": serialize_dataclass(updated)}


    def _live_supervisor_records(self, project_id: str) -> list[dict[str, object]]:
        control_dir = self._supervisor_control_dir()
        if not control_dir.exists():
            return []
        live_records: list[dict[str, object]] = []
        for path in sorted(control_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            pid = int(payload.get("pid") or 0)
            if pid <= 0:
                continue
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            record_project_id = str(payload.get("project_id") or "").strip()
            if record_project_id and record_project_id != project_id:
                continue
            live_records.append(payload)
        return live_records


    def _supervisor_control_dir(self) -> Path:
        return self.ctx.config.db_path.parent / "supervisors"


    def _resolve_source_root(self, project_id: str) -> Path:
        """Resolve the source repo root for a project."""
        project = self.store.get_project(project_id)
        if project and project.adapter_name == "current_repo_git_worktree":
            configured = os.environ.get("ACCRUVIA_SOURCE_REPO_ROOT")
            if configured:
                return Path(configured).resolve()
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    check=True, capture_output=True, text=True,
                )
                return Path(result.stdout.strip())
            except Exception:
                pass
        return Path(__file__).resolve().parents[2]


    def _workflow_status_for_objective(
        self,
        objective: Objective,
        linked_tasks: list[Task],
        promotion_review: dict[str, object],
        repo_promotion: dict[str, object],
    ) -> dict[str, object]:
        planning = self.workflow_service.planning_readiness(objective.id)
        execution = self.workflow_service.execution_readiness(objective.id, linked_tasks)
        review = self.workflow_service.review_readiness(objective.id, linked_tasks)
        promotion_checks = [
            {
                "key": "review_clear",
                "label": "Objective review clear",
                "ok": bool(promotion_review.get("review_clear")),
                "detail": "" if bool(promotion_review.get("review_clear")) else str(promotion_review.get("next_action") or "Objective review is not clear yet."),
            },
            {
                "key": "repo_promotion_eligible",
                "label": "Repo promotion eligible",
                "ok": bool(repo_promotion.get("eligible")),
                "detail": "" if bool(repo_promotion.get("eligible")) else str(repo_promotion.get("reason") or "Repo promotion is not eligible yet."),
            },
        ]
        promotion = {
            "stage": "promotion",
            "ready": all(bool(check["ok"]) for check in promotion_checks),
            "checks": promotion_checks,
        }
        current_stage = (
            "promotion"
            if objective.status == ObjectiveStatus.RESOLVED and bool(promotion_review.get("review_rounds"))
            else "review"
            if objective.status == ObjectiveStatus.RESOLVED
            else "execution"
            if objective.status == ObjectiveStatus.EXECUTING
            else "planning"
        )
        return {
            "current_stage": current_stage,
            "planning": {"ready": planning.ready, "checks": _to_jsonable(planning.checks)},
            "execution": {"ready": execution.ready, "checks": _to_jsonable(execution.checks)},
            "review": {"ready": review.ready, "checks": _to_jsonable(review.checks)},
            "promotion": promotion,
        }

