from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain import Artifact, Task


@dataclass(slots=True)
class ValidationIssue:
    code: str
    summary: str
    details: dict[str, object]
    follow_on_title: str | None = None
    follow_on_objective: str | None = None


@dataclass(slots=True)
class ValidationResult:
    validator: str
    ok: bool
    summary: str
    issues: list[ValidationIssue]


class PromotionValidator(Protocol):
    def validate(self, task: Task, artifacts: list[Artifact]) -> ValidationResult: ...
