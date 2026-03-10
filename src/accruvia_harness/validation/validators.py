from __future__ import annotations

import json
from pathlib import Path

from ..domain import Artifact, Task
from .base import PromotionValidator, ValidationIssue, ValidationResult


class RequiredArtifactsValidator:
    def validate(self, task: Task, artifacts: list[Artifact]) -> ValidationResult:
        kinds = {artifact.kind for artifact in artifacts}
        missing = sorted(set(task.required_artifacts) - kinds)
        if not missing:
            return ValidationResult("required_artifacts", True, "Required artifacts are present.", [])
        return ValidationResult(
            "required_artifacts",
            False,
            "Required artifacts are missing.",
            [
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
        missing_paths = [artifact.path for artifact in artifacts if not Path(artifact.path).exists()]
        if not missing_paths:
            return ValidationResult("artifact_paths", True, "Artifact files exist on disk.", [])
        return ValidationResult(
            "artifact_paths",
            False,
            "Artifact files are missing on disk.",
            [
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
            return ValidationResult("report_artifact", True, "No report artifact available for structured validation.", [])
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
            return ValidationResult("report_artifact", True, "Report artifacts passed structured validation.", [])
        return ValidationResult("report_artifact", False, "Report artifacts failed structured validation.", issues)


def default_promotion_validators() -> list[PromotionValidator]:
    return [
        RequiredArtifactsValidator(),
        ArtifactPathValidator(),
        ReportArtifactValidator(),
    ]
