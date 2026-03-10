from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .domain import Artifact, Task


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


class RequiredArtifactsValidator:
    def validate(self, task: Task, artifacts: list[Artifact]) -> ValidationResult:
        kinds = {artifact.kind for artifact in artifacts}
        missing = sorted(set(task.required_artifacts) - kinds)
        if not missing:
            return ValidationResult(
                validator="required_artifacts",
                ok=True,
                summary="Required artifacts are present.",
                issues=[],
            )
        return ValidationResult(
            validator="required_artifacts",
            ok=False,
            summary="Required artifacts are missing.",
            issues=[
                ValidationIssue(
                    code="missing_required_artifacts",
                    summary="Promotion candidate is missing required artifacts.",
                    details={"missing": missing},
                    follow_on_title=f"Restore missing artifacts for {task.title}",
                    follow_on_objective=f"Produce the missing required artifacts: {', '.join(missing)}.",
                )
            ],
        )


class ArtifactPathValidator:
    def validate(self, task: Task, artifacts: list[Artifact]) -> ValidationResult:
        missing_paths: list[str] = []
        for artifact in artifacts:
            if not Path(artifact.path).exists():
                missing_paths.append(artifact.path)
        if not missing_paths:
            return ValidationResult(
                validator="artifact_paths",
                ok=True,
                summary="Artifact files exist on disk.",
                issues=[],
            )
        return ValidationResult(
            validator="artifact_paths",
            ok=False,
            summary="Artifact files are missing on disk.",
            issues=[
                ValidationIssue(
                    code="artifact_path_missing",
                    summary="Stored artifact path no longer exists.",
                    details={"missing_paths": missing_paths},
                    follow_on_title=f"Repair persisted artifacts for {task.title}",
                    follow_on_objective="Regenerate the missing persisted artifacts so promotion can validate them.",
                )
            ],
        )


class ReportArtifactValidator:
    def validate(self, task: Task, artifacts: list[Artifact]) -> ValidationResult:
        reports = [artifact for artifact in artifacts if artifact.kind == "report"]
        if not reports:
            return ValidationResult(
                validator="report_artifact",
                ok=True,
                summary="No report artifact available for structured validation.",
                issues=[],
            )

        issues: list[ValidationIssue] = []
        for artifact in reports:
            report_path = Path(artifact.path)
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                issues.append(
                    ValidationIssue(
                        code="report_unreadable",
                        summary="Report artifact is unreadable or invalid JSON.",
                        details={"path": artifact.path, "error": str(exc)},
                        follow_on_title=f"Repair invalid report for {task.title}",
                        follow_on_objective="Generate a valid structured report artifact for promotion review.",
                    )
                )
                continue
            if payload.get("promotion_blocked") is True:
                issues.append(
                    ValidationIssue(
                        code="report_marked_blocked",
                        summary=str(payload.get("promotion_block_reason", "Report marked promotion blocked.")),
                        details=payload,
                        follow_on_title=payload.get("follow_on_title") or f"Resolve promotion blocker for {task.title}",
                        follow_on_objective=payload.get("follow_on_objective")
                        or "Address the blocker recorded in the report artifact and regenerate the candidate.",
                    )
                )

        if not issues:
            return ValidationResult(
                validator="report_artifact",
                ok=True,
                summary="Report artifacts passed structured validation.",
                issues=[],
            )
        return ValidationResult(
            validator="report_artifact",
            ok=False,
            summary="Report artifacts failed structured validation.",
            issues=issues,
        )


def default_promotion_validators() -> list[PromotionValidator]:
    return [
        RequiredArtifactsValidator(),
        ArtifactPathValidator(),
        ReportArtifactValidator(),
    ]
