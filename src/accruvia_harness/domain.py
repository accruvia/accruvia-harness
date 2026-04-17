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


class GlobalSystemState(StrEnum):
    OFF = "off"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FROZEN = "frozen"


class ControlLaneStateValue(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    COOLDOWN = "cooldown"
    DISABLED = "disabled"


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


class ObjectivePhase(StrEnum):
    """Durable lifecycle phase for an objective.

    This is the workflow contract. Each phase has exactly one valid
    successor (except FAILED which is terminal). The Temporal workflow
    advances phases — no LLM, no UI handler, no background thread can
    skip a phase because the workflow code is the single caller of
    advance_objective_phase.

    The existing ObjectiveStatus enum is kept for backward compat with
    the UI and store. ObjectivePhase is the authoritative source of
    truth for where an objective is in its lifecycle.
    """
    CREATED = "created"
    INTERROGATING = "interrogating"
    MERMAID_REVIEW = "mermaid_review"
    TRIO_PLANNING = "trio_planning"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    PROMOTED = "promoted"
    FAILED = "failed"


_PHASE_SEQUENCE: list[ObjectivePhase] = [
    ObjectivePhase.CREATED,
    ObjectivePhase.INTERROGATING,
    ObjectivePhase.MERMAID_REVIEW,
    ObjectivePhase.TRIO_PLANNING,
    ObjectivePhase.EXECUTING,
    ObjectivePhase.REVIEWING,
    ObjectivePhase.PROMOTED,
]

VALID_PHASE_TRANSITIONS: dict[ObjectivePhase, frozenset[ObjectivePhase]] = {
    phase: frozenset({_PHASE_SEQUENCE[i + 1], ObjectivePhase.FAILED})
    for i, phase in enumerate(_PHASE_SEQUENCE[:-1])
}
VALID_PHASE_TRANSITIONS[ObjectivePhase.PROMOTED] = frozenset()
VALID_PHASE_TRANSITIONS[ObjectivePhase.FAILED] = frozenset()

PHASE_TO_STATUS: dict[ObjectivePhase, ObjectiveStatus] = {
    ObjectivePhase.CREATED: ObjectiveStatus.OPEN,
    ObjectivePhase.INTERROGATING: ObjectiveStatus.INVESTIGATING,
    ObjectivePhase.MERMAID_REVIEW: ObjectiveStatus.PLANNING,
    ObjectivePhase.TRIO_PLANNING: ObjectiveStatus.PLANNING,
    ObjectivePhase.EXECUTING: ObjectiveStatus.EXECUTING,
    ObjectivePhase.REVIEWING: ObjectiveStatus.RESOLVED,
    ObjectivePhase.PROMOTED: ObjectiveStatus.RESOLVED,
    ObjectivePhase.FAILED: ObjectiveStatus.PAUSED,
}


def advance_objective_phase(
    current: ObjectivePhase, target: ObjectivePhase,
) -> ObjectivePhase:
    """Validate and return the target phase.

    Raises ValueError if the transition is illegal. This is the single
    enforcement point — the workflow calls this before persisting.
    """
    if current == target:
        return target
    allowed = VALID_PHASE_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ValueError(
            f"Illegal phase transition: {current.value} -> {target.value}. "
            f"Allowed: {sorted(p.value for p in allowed)}"
        )
    return target


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
    plan_id: str | None = None
    mermaid_node_id: str | None = None
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
    max_attempts: int = 10
    max_branches: int = 1
    required_artifacts: list[str] = field(default_factory=lambda: ["plan", "report"])
    attempt_metadata: dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Plan:
    """Approved atomic slice derived from an objective. See specs/atomic-plan-schema.md."""

    id: str
    objective_id: str
    mermaid_node_id: str | None = None
    parent_plan_id: str | None = None
    plan_revision: int = 1
    slice: dict[str, Any] = field(default_factory=dict)
    atomicity_assessment: dict[str, Any] = field(default_factory=dict)
    approval_status: str = "approved"
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
    phase: ObjectivePhase = ObjectivePhase.CREATED
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
class DecisionQueueItem:
    id: str
    run_id: str
    task_id: str
    evaluation_id: str
    priority: int = 100
    created_at: datetime = field(default_factory=utc_now)
    status: str = "pending"
    started_at: datetime | None = None
    completed_at: datetime | None = None


class FailureCategory(str):
    """String-backed failure category with enum-like `.value` access."""

    def __new__(cls, value: str) -> "FailureCategory":
        return str.__new__(cls, str(value).strip())

    @property
    def value(self) -> str:
        return str(self)


@dataclass(slots=True)
class FailurePatternRecord:
    id: str
    task_id: str
    run_id: str
    objective_id: str | None = None
    attempt: int = 1
    category: FailureCategory = FailureCategory("")
    fingerprint: str = ""
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
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


@dataclass(slots=True)
class ControlSystemState:
    id: str = "system"
    global_state: GlobalSystemState = GlobalSystemState.OFF
    master_switch: bool = False
    freeze_reason: str | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ControlLaneState:
    lane_name: str
    state: ControlLaneStateValue = ControlLaneStateValue.PAUSED
    reason: str | None = None
    cooldown_until: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ControlEvent:
    id: str
    event_type: str
    entity_type: str
    entity_id: str
    producer: str
    payload: dict[str, Any]
    idempotency_key: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ControlBreadcrumb:
    id: str
    entity_type: str
    entity_id: str
    worker_run_id: str | None = None
    classification: str | None = None
    path: str = ""
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ControlRecoveryAction:
    id: str
    action_type: str
    target_type: str
    target_id: str
    reason: str
    result: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ControlCooldown:
    id: str
    scope_type: str
    scope_id: str
    reason: str
    until_at: datetime
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ControlBudget:
    id: str
    budget_scope: str
    budget_key: str
    window_start: datetime
    window_end: datetime
    usage_count: int = 0
    usage_cost_usd: float = 0.0
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ControlWorkerRun:
    id: str
    task_id: str | None = None
    objective_id: str | None = None
    worker_kind: str = "coding_worker"
    runtime_name: str = "local"
    model_name: str | None = None
    attempt: int = 1
    status: str = "started"
    classification: str | None = None
    started_at: datetime = field(default_factory=utc_now)
    ended_at: datetime | None = None
    breadcrumb_path: str | None = None


@dataclass(slots=True)
class FailureClassification:
    classification: str
    confidence: float
    retry_recommended: bool
    cooldown_seconds: int = 0
    evidence: list[str] = field(default_factory=list)


def serialize_dataclass(value: Any) -> dict[str, Any]:
    payload = dict(value) if isinstance(value, dict) else asdict(value)
    for key, item in list(payload.items()):
        if isinstance(item, datetime):
            payload[key] = item.isoformat()
        elif isinstance(item, StrEnum):
            payload[key] = item.value
    return payload


# ---------------------------------------------------------------------------
# Typed domain classes — replace god dicts with structured data
# ---------------------------------------------------------------------------


class ReviewVerdict(StrEnum):
    PASS = "pass"
    CONCERN = "concern"
    REMEDIATION_REQUIRED = "remediation_required"


class ReviewSeverity(StrEnum):
    NONE = ""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewProgressStatus(StrEnum):
    NEW_CONCERN = "new_concern"
    STILL_BLOCKING = "still_blocking"
    IMPROVING = "improving"
    RESOLVED = "resolved"
    NOT_APPLICABLE = "not_applicable"


class ReviewDimension(StrEnum):
    INTENT_FIDELITY = "intent_fidelity"
    UNIT_TEST_COVERAGE = "unit_test_coverage"
    INTEGRATION_E2E_COVERAGE = "integration_e2e_coverage"
    SECURITY = "security"
    DEVOPS = "devops"
    ATOMIC_FIDELITY = "atomic_fidelity"
    CODE_STRUCTURE = "code_structure"


class PlanComplexity(StrEnum):
    TRIVIAL = "trivial"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    TOO_LARGE = "too_large"


class OrphanStrategy(StrEnum):
    ABSORB = "absorb"
    FOLLOW_UP = "follow_up"
    ACCEPT = "accept"


def _safe_enum(enum_cls, value, default=None):
    """Parse a StrEnum value leniently — invalid/missing values return the default."""
    if value is None or value == "":
        return default
    try:
        return enum_cls(str(value).strip().lower())
    except (ValueError, KeyError):
        return default


@dataclass(slots=True)
class ArtifactSchema:
    type: str = ""
    description: str = ""
    required_fields: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ArtifactSchema":
        if not d or not isinstance(d, dict):
            return cls()
        return cls(
            type=str(d.get("type") or ""),
            description=str(d.get("description") or ""),
            required_fields=list(d.get("required_fields") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "description": self.description,
            "required_fields": list(self.required_fields),
        }


@dataclass(slots=True)
class EvidenceContract:
    required_artifact_type: str = ""
    artifact_schema: ArtifactSchema | None = None
    closure_criteria: str = ""
    evidence_required: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "EvidenceContract":
        if not d or not isinstance(d, dict):
            return cls()
        schema_raw = d.get("artifact_schema")
        return cls(
            required_artifact_type=str(d.get("required_artifact_type") or ""),
            artifact_schema=ArtifactSchema.from_dict(schema_raw) if schema_raw else None,
            closure_criteria=str(d.get("closure_criteria") or ""),
            evidence_required=str(d.get("evidence_required") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_artifact_type": self.required_artifact_type,
            "artifact_schema": self.artifact_schema.to_dict() if self.artifact_schema else None,
            "closure_criteria": self.closure_criteria,
            "evidence_required": self.evidence_required,
        }


@dataclass(slots=True)
class ReviewPacket:
    reviewer: str = ""
    dimension: str = ""
    verdict: ReviewVerdict = ReviewVerdict.CONCERN
    progress_status: str = ""
    severity: str = ""
    owner_scope: str = ""
    summary: str = ""
    findings: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    required_artifact_type: str = ""
    artifact_schema: dict[str, Any] = field(default_factory=dict)
    evidence_contract: dict[str, Any] = field(default_factory=dict)
    closure_criteria: str = ""
    evidence_required: str = ""
    repeat_reason: str = ""
    llm_usage: dict[str, Any] = field(default_factory=dict)
    llm_usage_reported: bool = False
    llm_usage_source: str = ""
    backend: str = ""
    prompt_path: str = ""
    response_path: str = ""
    review_task_id: str = ""
    review_run_id: str = ""
    packet_record_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ReviewPacket":
        if not d or not isinstance(d, dict):
            return cls()
        verdict = _safe_enum(ReviewVerdict, d.get("verdict"), ReviewVerdict.CONCERN)
        return cls(
            reviewer=str(d.get("reviewer") or ""),
            dimension=str(d.get("dimension") or ""),
            verdict=verdict,
            progress_status=str(d.get("progress_status") or ""),
            severity=str(d.get("severity") or ""),
            owner_scope=str(d.get("owner_scope") or ""),
            summary=str(d.get("summary") or ""),
            findings=list(d.get("findings") or []),
            evidence=list(d.get("evidence") or []),
            required_artifact_type=str(d.get("required_artifact_type") or ""),
            artifact_schema=dict(d.get("artifact_schema") or {}),
            evidence_contract=dict(d.get("evidence_contract") or {}),
            closure_criteria=str(d.get("closure_criteria") or ""),
            evidence_required=str(d.get("evidence_required") or ""),
            repeat_reason=str(d.get("repeat_reason") or ""),
            llm_usage=dict(d.get("llm_usage") or {}),
            llm_usage_reported=bool(d.get("llm_usage_reported") or False),
            llm_usage_source=str(d.get("llm_usage_source") or ""),
            backend=str(d.get("backend") or ""),
            prompt_path=str(d.get("prompt_path") or ""),
            response_path=str(d.get("response_path") or ""),
            review_task_id=str(d.get("review_task_id") or ""),
            review_run_id=str(d.get("review_run_id") or ""),
            packet_record_id=str(d.get("packet_record_id") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer": self.reviewer,
            "dimension": self.dimension,
            "verdict": self.verdict.value if isinstance(self.verdict, ReviewVerdict) else str(self.verdict),
            "progress_status": self.progress_status,
            "severity": self.severity,
            "owner_scope": self.owner_scope,
            "summary": self.summary,
            "findings": list(self.findings),
            "evidence": list(self.evidence),
            "required_artifact_type": self.required_artifact_type,
            "artifact_schema": dict(self.artifact_schema),
            "evidence_contract": dict(self.evidence_contract),
            "closure_criteria": self.closure_criteria,
            "evidence_required": self.evidence_required,
            "repeat_reason": self.repeat_reason,
            "llm_usage": dict(self.llm_usage),
            "llm_usage_reported": self.llm_usage_reported,
            "llm_usage_source": self.llm_usage_source,
            "backend": self.backend,
            "prompt_path": self.prompt_path,
            "response_path": self.response_path,
            "review_task_id": self.review_task_id,
            "review_run_id": self.review_run_id,
            "packet_record_id": self.packet_record_id,
        }


@dataclass(slots=True)
class PlanSlice:
    label: str = ""
    dependencies: list[str] = field(default_factory=list)
    derived_from: str = ""
    local_id: str = ""
    target_impl: str = ""
    target_test: str = ""
    transformation: str = ""
    input_samples: list[Any] = field(default_factory=list)
    output_samples: list[Any] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    orphan_strategy: OrphanStrategy | None = None
    orphan_acceptance_reason: str = ""
    risks: list[str] = field(default_factory=list)
    estimated_complexity: PlanComplexity = PlanComplexity.MEDIUM
    creates_new_file: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "PlanSlice":
        if not d or not isinstance(d, dict):
            return cls()
        return cls(
            label=str(d.get("label") or ""),
            dependencies=list(d.get("dependencies") or []),
            derived_from=str(d.get("derived_from") or ""),
            local_id=str(d.get("local_id") or ""),
            target_impl=str(d.get("target_impl") or ""),
            target_test=str(d.get("target_test") or ""),
            transformation=str(d.get("transformation") or ""),
            input_samples=list(d.get("input_samples") or []),
            output_samples=list(d.get("output_samples") or []),
            resources=list(d.get("resources") or []),
            supersedes=list(d.get("supersedes") or []),
            orphan_strategy=_safe_enum(OrphanStrategy, d.get("orphan_strategy")),
            orphan_acceptance_reason=str(d.get("orphan_acceptance_reason") or ""),
            risks=list(d.get("risks") or []),
            estimated_complexity=_safe_enum(PlanComplexity, d.get("estimated_complexity"), PlanComplexity.MEDIUM),
            creates_new_file=bool(d.get("creates_new_file") or False),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "label": self.label,
            "dependencies": list(self.dependencies),
            "derived_from": self.derived_from,
            "local_id": self.local_id,
        }
        if self.target_impl:
            d["target_impl"] = self.target_impl
        if self.target_test:
            d["target_test"] = self.target_test
        if self.transformation:
            d["transformation"] = self.transformation
        if self.input_samples:
            d["input_samples"] = list(self.input_samples)
        if self.output_samples:
            d["output_samples"] = list(self.output_samples)
        if self.resources:
            d["resources"] = list(self.resources)
        if self.supersedes:
            d["supersedes"] = list(self.supersedes)
        if self.orphan_strategy is not None:
            d["orphan_strategy"] = self.orphan_strategy.value
        if self.orphan_acceptance_reason:
            d["orphan_acceptance_reason"] = self.orphan_acceptance_reason
        if self.risks:
            d["risks"] = list(self.risks)
        if self.estimated_complexity != PlanComplexity.MEDIUM:
            d["estimated_complexity"] = self.estimated_complexity.value
        if self.creates_new_file:
            d["creates_new_file"] = True
        return d


@dataclass(slots=True)
class InterrogationReview:
    completed: bool = False
    summary: str = ""
    plan_elements: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    generated_by: str = "deterministic"
    backend: str | None = None
    prompt_path: str | None = None
    response_path: str | None = None
    red_team_rounds: int | None = None
    red_team_stop_reason: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "InterrogationReview":
        if not d or not isinstance(d, dict):
            return cls()
        return cls(
            completed=bool(d.get("completed") or False),
            summary=str(d.get("summary") or ""),
            plan_elements=list(d.get("plan_elements") or []),
            questions=list(d.get("questions") or []),
            generated_by=str(d.get("generated_by") or "deterministic"),
            backend=d.get("backend"),
            prompt_path=d.get("prompt_path"),
            response_path=d.get("response_path"),
            red_team_rounds=d.get("red_team_rounds"),
            red_team_stop_reason=d.get("red_team_stop_reason"),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "completed": self.completed,
            "summary": self.summary,
            "plan_elements": list(self.plan_elements),
            "questions": list(self.questions),
            "generated_by": self.generated_by,
        }
        if self.backend is not None:
            d["backend"] = self.backend
        if self.prompt_path is not None:
            d["prompt_path"] = self.prompt_path
        if self.response_path is not None:
            d["response_path"] = self.response_path
        if self.red_team_rounds is not None:
            d["red_team_rounds"] = self.red_team_rounds
        if self.red_team_stop_reason is not None:
            d["red_team_stop_reason"] = self.red_team_stop_reason
        return d
