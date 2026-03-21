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


VALID_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.ACTIVE, TaskStatus.COMPLETED, TaskStatus.FAILED}),
    TaskStatus.ACTIVE: frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PENDING}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset({TaskStatus.PENDING}),
}


def validate_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    if current == target:
        return  # idempotent no-op
    allowed = VALID_TASK_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ValueError(f"Invalid task transition: {current} -> {target}")


class RunStatus(StrEnum):
    PLANNING = "planning"
    WORKING = "working"
    VALIDATING = "validating"
    ANALYZING = "analyzing"
    DECIDING = "deciding"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    DISPOSED = "disposed"


class EvaluationVerdict(StrEnum):
    ACCEPTABLE = "acceptable"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    BLOCKED = "blocked"


class DecisionAction(StrEnum):
    RETRY = "retry"
    PROMOTE = "promote"
    FAIL = "fail"
    BRANCH = "branch"


class PromotionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class WorkspacePolicy(StrEnum):
    ISOLATED_REQUIRED = "isolated_required"
    ISOLATED_PREFERRED = "isolated_preferred"
    SHARED_ALLOWED = "shared_allowed"


class PromotionMode(StrEnum):
    DIRECT_MAIN = "direct_main"
    BRANCH_ONLY = "branch_only"
    BRANCH_AND_PR = "branch_and_pr"


class RepoProvider(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"


class ObjectiveStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    PLANNING = "planning"
    EXECUTING = "executing"
    PAUSED = "paused"
    RESOLVED = "resolved"


VALID_OBJECTIVE_TRANSITIONS: dict[ObjectiveStatus, frozenset[ObjectiveStatus]] = {
    ObjectiveStatus.OPEN: frozenset({ObjectiveStatus.INVESTIGATING, ObjectiveStatus.PLANNING, ObjectiveStatus.PAUSED, ObjectiveStatus.RESOLVED}),
    ObjectiveStatus.INVESTIGATING: frozenset({ObjectiveStatus.PLANNING, ObjectiveStatus.PAUSED, ObjectiveStatus.RESOLVED}),
    ObjectiveStatus.PLANNING: frozenset({ObjectiveStatus.EXECUTING, ObjectiveStatus.INVESTIGATING, ObjectiveStatus.PAUSED, ObjectiveStatus.RESOLVED}),
    ObjectiveStatus.EXECUTING: frozenset({ObjectiveStatus.PLANNING, ObjectiveStatus.PAUSED, ObjectiveStatus.RESOLVED}),
    ObjectiveStatus.PAUSED: frozenset({ObjectiveStatus.OPEN, ObjectiveStatus.INVESTIGATING, ObjectiveStatus.PLANNING, ObjectiveStatus.EXECUTING, ObjectiveStatus.RESOLVED}),
    ObjectiveStatus.RESOLVED: frozenset(),
}


def validate_objective_transition(current: ObjectiveStatus, target: ObjectiveStatus) -> None:
    if current == target:
        return  # idempotent no-op
    allowed = VALID_OBJECTIVE_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ValueError(f"Invalid objective transition: {current} -> {target}")


class MermaidStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PAUSED = "paused"
    FINISHED = "finished"
    SUPERSEDED = "superseded"


@dataclass(slots=True)
class Project:
    id: str
    name: str
    description: str
    adapter_name: str = "generic"
    workspace_policy: WorkspacePolicy = WorkspacePolicy.ISOLATED_REQUIRED
    promotion_mode: PromotionMode = PromotionMode.BRANCH_AND_PR
    repo_provider: RepoProvider | None = None
    repo_name: str | None = None
    base_branch: str = "main"
    max_concurrent_tasks: int = 1
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Task:
    id: str
    project_id: str
    title: str
    objective: str
    objective_id: str | None = None
    priority: int = 100
    parent_task_id: str | None = None
    source_run_id: str | None = None
    external_ref_type: str | None = None
    external_ref_id: str | None = None
    external_ref_metadata: dict[str, Any] = field(default_factory=dict)
    validation_profile: str = "generic"
    validation_mode: str = "default_focused"
    scope: dict[str, Any] = field(default_factory=dict)
    strategy: str = "default"
    max_attempts: int = 3
    max_branches: int = 1
    required_artifacts: list[str] = field(default_factory=lambda: ["plan", "report"])
    attempt_metadata: dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Objective:
    id: str
    project_id: str
    title: str
    summary: str
    priority: int = 100
    status: ObjectiveStatus = ObjectiveStatus.OPEN
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class IntentModel:
    id: str
    objective_id: str
    version: int
    intent_summary: str
    success_definition: str = ""
    non_negotiables: list[str] = field(default_factory=list)
    preferred_tradeoffs: list[str] = field(default_factory=list)
    unacceptable_outcomes: list[str] = field(default_factory=list)
    known_unknowns: list[str] = field(default_factory=list)
    operator_examples: list[str] = field(default_factory=list)
    frustration_signals: list[str] = field(default_factory=list)
    sop_constraints: list[str] = field(default_factory=list)
    current_confidence: float = 0.0
    author_type: str = "operator"
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class MermaidArtifact:
    id: str
    objective_id: str
    diagram_type: str
    version: int
    status: MermaidStatus
    summary: str
    content: str
    required_for_execution: bool = False
    blocking_reason: str = ""
    author_type: str = "operator"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Run:
    id: str
    task_id: str
    status: RunStatus
    attempt: int
    summary: str
    branch_id: str | None = None
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
    verdict: EvaluationVerdict
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


@dataclass(slots=True)
class ContextRecord:
    id: str
    record_type: str
    project_id: str
    objective_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    visibility: str = "model_visible"
    author_type: str = "system"
    author_id: str = ""
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)


def serialize_dataclass(value: Any) -> dict[str, Any]:
    payload = asdict(value)
    for key, item in list(payload.items()):
        if isinstance(item, datetime):
            payload[key] = item.isoformat()
        elif isinstance(item, StrEnum):
            payload[key] = item.value
    return payload
