from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class TaskStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class RunStatus(StrEnum):
    PLANNING = "planning"
    WORKING = "working"
    ANALYZING = "analyzing"
    DECIDING = "deciding"
    COMPLETED = "completed"
    FAILED = "failed"


class DecisionAction(StrEnum):
    RETRY = "retry"
    PROMOTE = "promote"
    FAIL = "fail"
    BRANCH = "branch"


class PromotionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(slots=True)
class Project:
    id: str
    name: str
    description: str
    adapter_name: str = "generic"
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Task:
    id: str
    project_id: str
    title: str
    objective: str
    priority: int = 100
    parent_task_id: str | None = None
    source_run_id: str | None = None
    external_ref_type: str | None = None
    external_ref_id: str | None = None
    validation_profile: str = "generic"
    strategy: str = "default"
    max_attempts: int = 3
    required_artifacts: list[str] = field(default_factory=lambda: ["plan", "report"])
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Run:
    id: str
    task_id: str
    status: RunStatus
    attempt: int
    summary: str
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Artifact:
    id: str
    run_id: str
    kind: str
    path: str
    summary: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Evaluation:
    id: str
    run_id: str
    verdict: str
    confidence: float
    summary: str
    details: dict[str, Any]
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Decision:
    id: str
    run_id: str
    action: DecisionAction
    rationale: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Event:
    id: str
    entity_type: str
    entity_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class TaskLease:
    task_id: str
    worker_id: str
    lease_expires_at: datetime
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class PromotionRecord:
    id: str
    task_id: str
    run_id: str
    status: PromotionStatus
    summary: str
    details: dict[str, Any]
    created_at: datetime = field(default_factory=utc_now)


def serialize_dataclass(value: Any) -> dict[str, Any]:
    payload = asdict(value)
    for key, item in list(payload.items()):
        if isinstance(item, datetime):
            payload[key] = item.isoformat()
        elif isinstance(item, StrEnum):
            payload[key] = item.value
    return payload
