from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime

from ..domain import (
    ControlBreadcrumb,
    ControlBudget,
    ControlCooldown,
    ControlEvent,
    ControlLaneState,
    ControlLaneStateValue,
    ControlRecoveryAction,
    ControlSystemState,
    ControlWorkerRun,
    ContextRecord,
    Decision,
    DecisionAction,
    DecisionQueueItem,
    EvaluationVerdict,
    Event,
    Evaluation,
    FailureCategory,
    FailurePatternRecord,
    GlobalSystemState,
    IntentModel,
    MermaidArtifact,
    MermaidStatus,
    Objective,
    ObjectiveStatus,
    PromotionRecord,
    PromotionStatus,
    Project,
    PromotionMode,
    Run,
    RepoProvider,
    RunStatus,
    Task,
    TaskLease,
    TaskStatus,
    WorkspacePolicy,
)

logger = logging.getLogger(__name__)


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _safe_json_loads(value: str | None, fallback: object = None, *, column: str = "unknown") -> object:
    """Parse JSON with a fallback for corrupt data instead of crashing."""
    if value is None:
        return fallback if fallback is not None else {}
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error(
            "DATA_CORRUPTION: corrupt JSON in column %s (returning fallback): %s — raw value: %.200s",
            column, exc, value,
        )
        return fallback if fallback is not None else {}


def task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        project_id=row["project_id"],
        objective_id=row["objective_id"] if "objective_id" in row.keys() else None,
        title=row["title"],
        objective=row["objective"],
        priority=int(row["priority"]),
        parent_task_id=row["parent_task_id"],
        source_run_id=row["source_run_id"],
        external_ref_type=row["external_ref_type"],
        external_ref_id=row["external_ref_id"],
        external_ref_metadata=_safe_json_loads(row["external_ref_metadata_json"], {}, column="tasks.external_ref_metadata_json"),
        validation_profile=row["validation_profile"],
        validation_mode=row["validation_mode"] if "validation_mode" in row.keys() else "default_focused",
        scope=_safe_json_loads(row["scope_json"], {}, column="tasks.scope_json"),
        strategy=row["strategy"],
        max_attempts=int(row["max_attempts"]),
        max_branches=int(row["max_branches"]) if "max_branches" in row.keys() else 1,
        required_artifacts=_safe_json_loads(row["required_artifacts_json"], [], column="tasks.required_artifacts_json"),
        attempt_metadata=_safe_json_loads(row["attempt_metadata_json"], {}, column="tasks.attempt_metadata_json") if "attempt_metadata_json" in row.keys() else {},
        status=TaskStatus(row["status"]),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def run_from_row(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        task_id=row["task_id"],
        status=RunStatus(row["status"]),
        attempt=int(row["attempt"]),
        summary=row["summary"],
        branch_id=row["branch_id"] if "branch_id" in row.keys() else None,
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        adapter_name=row["adapter_name"],
        workspace_policy=WorkspacePolicy(
            row["workspace_policy"] if "workspace_policy" in row.keys() else WorkspacePolicy.ISOLATED_REQUIRED.value
        ),
        promotion_mode=PromotionMode(
            row["promotion_mode"] if "promotion_mode" in row.keys() else PromotionMode.BRANCH_AND_PR.value
        ),
        repo_provider=RepoProvider(row["repo_provider"]) if "repo_provider" in row.keys() and row["repo_provider"] else None,
        repo_name=row["repo_name"] if "repo_name" in row.keys() else None,
        base_branch=row["base_branch"] if "base_branch" in row.keys() else "main",
        max_concurrent_tasks=int(row["max_concurrent_tasks"]) if "max_concurrent_tasks" in row.keys() else 0,
        created_at=parse_dt(row["created_at"]),
    )


def task_lease_from_row(row: sqlite3.Row) -> TaskLease:
    return TaskLease(
        task_id=row["task_id"],
        worker_id=row["worker_id"],
        lease_expires_at=parse_dt(row["lease_expires_at"]),
        created_at=parse_dt(row["created_at"]),
    )


def objective_from_row(row: sqlite3.Row) -> Objective:
    return Objective(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        summary=row["summary"],
        priority=int(row["priority"]),
        status=ObjectiveStatus(row["status"]),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def control_system_state_from_row(row: sqlite3.Row) -> ControlSystemState:
    return ControlSystemState(
        id=row["id"],
        global_state=GlobalSystemState(row["global_state"]),
        master_switch=bool(int(row["master_switch"])),
        freeze_reason=row["freeze_reason"],
        updated_at=parse_dt(row["updated_at"]),
    )


def control_lane_state_from_row(row: sqlite3.Row) -> ControlLaneState:
    return ControlLaneState(
        lane_name=row["lane_name"],
        state=ControlLaneStateValue(row["state"]),
        reason=row["reason"],
        cooldown_until=parse_dt(row["cooldown_until"]) if row["cooldown_until"] else None,
        updated_at=parse_dt(row["updated_at"]),
    )


def control_cooldown_from_row(row: sqlite3.Row) -> ControlCooldown:
    return ControlCooldown(
        id=row["id"],
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        reason=row["reason"],
        until_at=parse_dt(row["until_at"]),
        created_at=parse_dt(row["created_at"]),
    )


def control_budget_from_row(row: sqlite3.Row) -> ControlBudget:
    return ControlBudget(
        id=row["id"],
        budget_scope=row["budget_scope"],
        budget_key=row["budget_key"],
        window_start=parse_dt(row["window_start"]),
        window_end=parse_dt(row["window_end"]),
        usage_count=int(row["usage_count"]),
        usage_cost_usd=float(row["usage_cost_usd"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def intent_model_from_row(row: sqlite3.Row) -> IntentModel:
    return IntentModel(
        id=row["id"],
        objective_id=row["objective_id"],
        version=int(row["version"]),
        intent_summary=row["intent_summary"],
        success_definition=row["success_definition"],
        non_negotiables=_safe_json_loads(row["non_negotiables_json"], [], column="intent_models.non_negotiables_json"),
        preferred_tradeoffs=_safe_json_loads(
            row["preferred_tradeoffs_json"], [], column="intent_models.preferred_tradeoffs_json"
        ),
        unacceptable_outcomes=_safe_json_loads(
            row["unacceptable_outcomes_json"], [], column="intent_models.unacceptable_outcomes_json"
        ),
        known_unknowns=_safe_json_loads(row["known_unknowns_json"], [], column="intent_models.known_unknowns_json"),
        operator_examples=_safe_json_loads(
            row["operator_examples_json"], [], column="intent_models.operator_examples_json"
        ),
        frustration_signals=_safe_json_loads(
            row["frustration_signals_json"], [], column="intent_models.frustration_signals_json"
        ),
        sop_constraints=_safe_json_loads(row["sop_constraints_json"], [], column="intent_models.sop_constraints_json"),
        current_confidence=float(row["current_confidence"]),
        author_type=row["author_type"],
        created_at=parse_dt(row["created_at"]),
    )


def mermaid_artifact_from_row(row: sqlite3.Row) -> MermaidArtifact:
    return MermaidArtifact(
        id=row["id"],
        objective_id=row["objective_id"],
        diagram_type=row["diagram_type"],
        version=int(row["version"]),
        status=MermaidStatus(row["status"]),
        summary=row["summary"],
        content=row["content"],
        required_for_execution=bool(int(row["required_for_execution"])),
        blocking_reason=row["blocking_reason"],
        author_type=row["author_type"],
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def evaluation_from_row(row: sqlite3.Row) -> Evaluation:
    return Evaluation(
        id=row["id"],
        run_id=row["run_id"],
        verdict=EvaluationVerdict(row["verdict"]),
        confidence=float(row["confidence"]),
        summary=row["summary"],
        details=_safe_json_loads(row["details_json"], {}, column="evaluations.details_json"),
        created_at=parse_dt(row["created_at"]),
    )


def decision_from_row(row: sqlite3.Row) -> Decision:
    return Decision(
        id=row["id"],
        run_id=row["run_id"],
        action=DecisionAction(row["action"]),
        rationale=row["rationale"],
        created_at=parse_dt(row["created_at"]),
    )


def event_from_row(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        event_type=row["event_type"],
        payload=_safe_json_loads(row["payload_json"], {}, column="events.payload_json"),
        created_at=parse_dt(row["created_at"]),
    )


def control_event_from_row(row: sqlite3.Row) -> ControlEvent:
    return ControlEvent(
        id=row["id"],
        event_type=row["event_type"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        producer=row["producer"],
        payload=_safe_json_loads(row["payload_json"], {}, column="control_events.payload_json"),
        idempotency_key=row["idempotency_key"],
        created_at=parse_dt(row["created_at"]),
    )


def promotion_from_row(row: sqlite3.Row) -> PromotionRecord:
    return PromotionRecord(
        id=row["id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        status=PromotionStatus(row["status"]),
        summary=row["summary"],
        details=_safe_json_loads(row["details_json"], {}, column="promotions.details_json"),
        created_at=parse_dt(row["created_at"]),
    )


def failure_pattern_from_row(row: sqlite3.Row) -> FailurePatternRecord:
    return FailurePatternRecord(
        id=row["id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        objective_id=row["objective_id"],
        attempt=int(row["attempt"]),
        category=FailureCategory(row["category"]),
        fingerprint=row["fingerprint"],
        summary=row["summary"],
        details=_safe_json_loads(row["details_json"], {}, column="failure_patterns.details_json"),
        created_at=parse_dt(row["created_at"]),
    )


def context_record_from_row(row: sqlite3.Row) -> ContextRecord:
    return ContextRecord(
        id=row["id"],
        record_type=row["record_type"],
        project_id=row["project_id"],
        objective_id=row["objective_id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        visibility=row["visibility"],
        author_type=row["author_type"],
        author_id=row["author_id"],
        content=row["content"],
        metadata=_safe_json_loads(row["metadata_json"], {}, column="context_records.metadata_json"),
        created_at=parse_dt(row["created_at"]),
    )


def decision_queue_item_from_row(row: sqlite3.Row) -> DecisionQueueItem:
    return DecisionQueueItem(
        id=row["id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        evaluation_id=row["evaluation_id"],
        priority=int(row["priority"]),
        created_at=parse_dt(row["created_at"]),
        status=row["status"],
        started_at=parse_dt(row["started_at"]) if row["started_at"] else None,
        completed_at=parse_dt(row["completed_at"]) if row["completed_at"] else None,
    )


def control_breadcrumb_from_row(row: sqlite3.Row) -> ControlBreadcrumb:
    return ControlBreadcrumb(
        id=row["id"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        worker_run_id=row["worker_run_id"],
        classification=row["classification"],
        path=row["path"],
        created_at=parse_dt(row["created_at"]),
    )


def control_recovery_action_from_row(row: sqlite3.Row) -> ControlRecoveryAction:
    return ControlRecoveryAction(
        id=row["id"],
        action_type=row["action_type"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        reason=row["reason"],
        result=row["result"],
        created_at=parse_dt(row["created_at"]),
    )


def control_worker_run_from_row(row: sqlite3.Row) -> ControlWorkerRun:
    return ControlWorkerRun(
        id=row["id"],
        task_id=row["task_id"],
        objective_id=row["objective_id"],
        worker_kind=row["worker_kind"],
        runtime_name=row["runtime_name"],
        model_name=row["model_name"],
        attempt=int(row["attempt"]),
        status=row["status"],
        classification=row["classification"],
        started_at=parse_dt(row["started_at"]),
        ended_at=parse_dt(row["ended_at"]) if row["ended_at"] else None,
        breadcrumb_path=row["breadcrumb_path"],
    )
