from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ..domain import (
    Decision,
    DecisionAction,
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


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


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
        validation_profile=row["validation_profile"],
        strategy=row["strategy"],
        max_attempts=int(row["max_attempts"]),
        required_artifacts=json.loads(row["required_artifacts_json"]),
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
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        adapter_name=row["adapter_name"],
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
        verdict=row["verdict"],
        confidence=float(row["confidence"]),
        summary=row["summary"],
        details=json.loads(row["details_json"]),
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
        payload=json.loads(row["payload_json"]),
        created_at=parse_dt(row["created_at"]),
    )


def promotion_from_row(row: sqlite3.Row) -> PromotionRecord:
    return PromotionRecord(
        id=row["id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        status=PromotionStatus(row["status"]),
        summary=row["summary"],
        details=json.loads(row["details_json"]),
        created_at=parse_dt(row["created_at"]),
    )
