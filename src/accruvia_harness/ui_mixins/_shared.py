"""Shared constants, helpers, and coordinator singletons for HarnessUIDataService mixins."""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, is_dataclass, asdict
from enum import Enum
from typing import Any

from ..ui_coordinators import (
    AtomicGenerationCoordinator,
    BackgroundSupervisorCoordinator,
    ObjectiveReviewCoordinator,
)

_ATOMIC_GENERATION = AtomicGenerationCoordinator()
_OBJECTIVE_REVIEW = ObjectiveReviewCoordinator()
_BACKGROUND_SUPERVISOR = BackgroundSupervisorCoordinator()

_MERMAID_RED_TEAM_MAX_ROUNDS = 20
_INTERROGATION_RED_TEAM_MAX_ROUNDS = 4
_ATOMIC_DECOMP_RED_TEAM_MAX_ROUNDS = 4

_OBJECTIVE_REVIEW_DIMENSIONS = frozenset(
    {
        "intent_fidelity",
        "unit_test_coverage",
        "integration_e2e_coverage",
        "security",
        "devops",
        "atomic_fidelity",
        "code_structure",
    }
)
_OBJECTIVE_REVIEW_VERDICTS = frozenset({"pass", "concern", "remediation_required"})
_OBJECTIVE_REVIEW_PROGRESS = frozenset(
    {"new_concern", "still_blocking", "improving", "resolved", "not_applicable"}
)
_OBJECTIVE_REVIEW_SEVERITIES = frozenset({"low", "medium", "high"})
_OBJECTIVE_REVIEW_REBUTTAL_OUTCOMES = frozenset(
    {"accepted", "wrong_artifact_type", "artifact_incomplete", "missing_terminal_event", "evidence_not_found"}
)
_TASK_REPLY_STALE_SECONDS = 90

_OBJECTIVE_REVIEW_VAGUE_PHRASES = (
    "improve",
    "better",
    "more coverage",
    "additional tests",
    "stronger evidence",
    "more evidence",
    "further validation",
    "review further",
    "be reviewed",
)


def _mermaid_node_id_for_task(task_id: str) -> str:
    suffix = task_id.split("_", 1)[-1][:12] if "_" in task_id else task_id[:12]
    return f"T_{suffix}"


class _AttrDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)
    def __setattr__(self, key, value):
        self[key] = value


ConversationTurn = _AttrDict  # type: ignore[assignment,misc]
ResponderResult = _AttrDict  # type: ignore[assignment,misc]
ResponderContextPacket = _AttrDict  # type: ignore[assignment,misc]


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


@dataclass(slots=True)
class RunOutputSection:
    label: str
    path: str
    content: str
