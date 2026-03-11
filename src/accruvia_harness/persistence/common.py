from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime

from ..domain import (
    Decision,
    DecisionAction,
    EvaluationVerdict,
    Event,
    Evaluation,
    PromotionRecord,
    PromotionStatus,
    Project,
    Run,
    RunStatus,
    Task,
    TaskLease,
    TaskStatus,
)

logger = logging.getLogger(__name__)


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _safe_json_loads(value: str | None, fallback: object = None) -> object:
    """Parse JSON with a fallback for corrupt data instead of crashing."""
    if value is None:
        return fallback if fallback is not None else {}
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Corrupt JSON in database column (returning fallback): %s", exc)
        return fallback if fallback is not None else {}


def task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        objective=row["objective"],
        priority=int(row["priority"]),
        parent_task_id=row["parent_task_id"],
        source_run_id=row["source_run_id"],
        external_ref_type=row["external_ref_type"],
        external_ref_id=row["external_ref_id"],
        external_ref_metadata=_safe_json_loads(row["external_ref_metadata_json"], {}),
        validation_profile=row["validation_profile"],
        strategy=row["strategy"],
        max_attempts=int(row["max_attempts"]),
        max_branches=int(row["max_branches"]) if "max_branches" in row.keys() else 1,
        required_artifacts=_safe_json_loads(row["required_artifacts_json"], []),
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


def evaluation_from_row(row: sqlite3.Row) -> Evaluation:
    return Evaluation(
        id=row["id"],
        run_id=row["run_id"],
        verdict=EvaluationVerdict(row["verdict"]),
        confidence=float(row["confidence"]),
        summary=row["summary"],
        details=_safe_json_loads(row["details_json"], {}),
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
        payload=_safe_json_loads(row["payload_json"], {}),
        created_at=parse_dt(row["created_at"]),
    )


def promotion_from_row(row: sqlite3.Row) -> PromotionRecord:
    return PromotionRecord(
        id=row["id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        status=PromotionStatus(row["status"]),
        summary=row["summary"],
        details=_safe_json_loads(row["details_json"], {}),
        created_at=parse_dt(row["created_at"]),
    )
