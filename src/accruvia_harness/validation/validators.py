from __future__ import annotations

import json
from pathlib import Path

from ..domain import Artifact, Task
from .base import PromotionValidator, ValidationIssue, ValidationResult


def _report_payloads(artifacts: list[Artifact]) -> tuple[list[dict[str, object]], list[ValidationIssue]]:
    payloads: list[dict[str, object]] = []
    issues: list[ValidationIssue] = []
    for artifact in artifacts:
        if artifact.kind != "report":
            continue
        report_path = Path(artifact.path)
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(
                ValidationIssue(
                    code="report_unreadable",
                    summary="Report artifact is unreadable or invalid JSON.",
                    details={"path": artifact.path, "error": str(exc)},
                    follow_on_title=f"Repair invalid report for {artifact.run_id}",
                    follow_on_objective="Generate a valid structured report artifact for promotion review.",
                )
            )
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads, issues


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
        payloads, issues = _report_payloads(artifacts)
        issues.extend(self._blocked_report_issues(task, payloads))
        if not issues:
            return ValidationResult("report_artifact", True, "Report artifacts passed structured validation.", [])
        return ValidationResult("report_artifact", False, "Report artifacts failed structured validation.", issues)

    def _blocked_report_issues(self, task: Task, payloads: list[dict[str, object]]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for payload in payloads:
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
        return issues


class ChangedFilesValidator:
    def validate(self, task: Task, artifacts: list[Artifact]) -> ValidationResult:
        payloads, issues = _report_payloads(artifacts)
        if issues:
            return ValidationResult("changed_files", False, "Unable to read report artifacts for changed-file validation.", issues)
        changed_files: list[str] = []
        for payload in payloads:
            candidate = payload.get("changed_files")
            if isinstance(candidate, list):
                changed_files.extend(str(item) for item in candidate if item)
        if changed_files:
            return ValidationResult(
                "changed_files",
                True,
                "Structured report records changed-file evidence.",
                [],
            )
        return ValidationResult(
            "changed_files",
            False,
            "Promotion candidate does not record changed-file evidence.",
            [
                ValidationIssue(
                    code="missing_changed_files",
                    summary="Structured promotion evidence does not show any changed files.",
                    details={"task_id": task.id},
                    follow_on_title=f"Record changed files for {task.title}",
                    follow_on_objective="Regenerate the candidate and include the changed source and test files in the structured report.",
                )
            ],
        )


class CompileCheckValidator:
    def validate(self, task: Task, artifacts: list[Artifact]) -> ValidationResult:
        payloads, issues = _report_payloads(artifacts)
        if issues:
            return ValidationResult("compile_check", False, "Unable to read report artifacts for compile validation.", issues)
        compile_checks = [payload.get("compile_check") for payload in payloads if isinstance(payload.get("compile_check"), dict)]
        passed = any(bool(check.get("passed")) for check in compile_checks)
        if passed:
            return ValidationResult("compile_check", True, "Compile check passed.", [])
        return ValidationResult(
            "compile_check",
            False,
            "Promotion candidate lacks a passing compile check.",
            [
                ValidationIssue(
                    code="compile_check_failed",
                    summary="Structured promotion evidence does not contain a passing compile check.",
                    details={"compile_checks": compile_checks},
                    follow_on_title=f"Repair compile check for {task.title}",
                    follow_on_objective="Regenerate the candidate and ensure the code compiles cleanly before promotion.",
                )
            ],
        )


class TestEvidenceValidator:
    def validate(self, task: Task, artifacts: list[Artifact]) -> ValidationResult:
        payloads, issues = _report_payloads(artifacts)
        if issues:
            return ValidationResult("test_evidence", False, "Unable to read report artifacts for test validation.", issues)
        test_files: list[str] = []
        test_checks: list[dict[str, object]] = []
        for payload in payloads:
            candidate_files = payload.get("test_files")
            if isinstance(candidate_files, list):
                test_files.extend(str(item) for item in candidate_files if item)
            candidate_check = payload.get("test_check")
            if isinstance(candidate_check, dict):
                test_checks.append(candidate_check)
        passed = any(bool(check.get("passed")) for check in test_checks)
        if test_files and passed:
            return ValidationResult("test_evidence", True, "Structured test evidence passed.", [])
        return ValidationResult(
            "test_evidence",
            False,
            "Promotion candidate lacks passing test evidence.",
            [
                ValidationIssue(
                    code="missing_test_evidence",
                    summary="Structured promotion evidence does not include passing tests and named test files.",
                    details={"test_files": test_files, "test_checks": test_checks},
                    follow_on_title=f"Add deterministic test evidence for {task.title}",
                    follow_on_objective="Regenerate the candidate with explicit test files and a passing deterministic test run in the structured report.",
                )
            ],
        )


class ValidationProfileEvidenceValidator:
    def validate(self, task: Task, artifacts: list[Artifact]) -> ValidationResult:
        payloads, issues = _report_payloads(artifacts)
        if issues:
            return ValidationResult(
                "validation_profile",
                False,
                "Unable to read report artifacts for validation-profile checks.",
                issues,
            )
        profile_values = {
            str(payload.get("validation_profile"))
            for payload in payloads
            if payload.get("validation_profile") is not None
        }
        if not profile_values:
            return ValidationResult(
                "validation_profile",
                True,
                "No report-level validation profile was declared; using task profile as authoritative.",
                [],
            )
        if task.validation_profile in profile_values:
            return ValidationResult(
                "validation_profile",
                True,
                "Structured report matches the task validation profile.",
                [],
            )
        return ValidationResult(
            "validation_profile",
            False,
            "Structured report does not match the task validation profile.",
            [
                ValidationIssue(
                    code="validation_profile_mismatch",
                    summary="The report-level validation profile does not match the task profile.",
                    details={"task_validation_profile": task.validation_profile, "report_profiles": sorted(profile_values)},
                    follow_on_title=f"Align validation profile for {task.title}",
                    follow_on_objective="Regenerate the candidate using the correct validation profile and evidence contract.",
                )
            ],
        )


class PythonTestFileValidator:
    def validate(self, task: Task, artifacts: list[Artifact]) -> ValidationResult:
        if task.validation_profile != "python":
            return ValidationResult("python_test_files", True, "Task is not using the python profile.", [])
        payloads, issues = _report_payloads(artifacts)
        if issues:
            return ValidationResult("python_test_files", False, "Unable to read report artifacts for python test validation.", issues)
        test_files: list[str] = []
        for payload in payloads:
            candidate_files = payload.get("test_files")
            if isinstance(candidate_files, list):
                test_files.extend(str(item) for item in candidate_files if item)
        if test_files and all(path.endswith(".py") for path in test_files):
            return ValidationResult("python_test_files", True, "Python profile test files look correct.", [])
        return ValidationResult(
            "python_test_files",
            False,
            "Python profile requires .py test files.",
            [
                ValidationIssue(
                    code="python_test_file_mismatch",
                    summary="Python validation profile requires Python test files.",
                    details={"test_files": test_files},
                    follow_on_title=f"Add Python test evidence for {task.title}",
                    follow_on_objective="Regenerate the candidate with Python test files that match the python validation profile.",
                )
            ],
        )


def validators_for_profile(profile: str) -> list[PromotionValidator]:
    validators: list[PromotionValidator] = [
        RequiredArtifactsValidator(),
        ArtifactPathValidator(),
        ValidationProfileEvidenceValidator(),
        ChangedFilesValidator(),
        CompileCheckValidator(),
        TestEvidenceValidator(),
        ReportArtifactValidator(),
    ]
    if profile == "python":
        validators.append(PythonTestFileValidator())
    return validators


def default_promotion_validators(profile: str = "generic") -> list[PromotionValidator]:
    return validators_for_profile(profile)
