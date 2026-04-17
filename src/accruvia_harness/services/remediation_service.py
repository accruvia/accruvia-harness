"""Remediation service for objective review findings.

Owns the creation of remediation tasks from review packets, evidence
contract extraction, rebuttal classification, and worker response
recording. Extracted from ObjectiveReviewMixin to separate business
logic from orchestration.
"""
from __future__ import annotations

from typing import Any

from ..domain import (
    ContextRecord,
    Objective,
    ReviewPacket,
    ReviewRound,
    ReviewVerdict,
    Task,
    TaskStatus,
    new_id,
)


class RemediationService:
    """Creates and manages remediation tasks from objective review findings."""

    def __init__(self, store: Any, task_service: Any) -> None:
        self.store = store
        self.task_service = task_service

    def create_remediation_tasks(
        self,
        objective: Objective,
        review_id: str,
        packets: list[dict[str, Any]],
    ) -> list[str]:
        linked_tasks = [
            task for task in self.store.list_tasks(objective.project_id)
            if task.objective_id == objective.id
        ]
        existing_dimensions: set[str] = set()
        for task in linked_tasks:
            metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
            remediation = metadata.get("objective_review_remediation") if isinstance(metadata.get("objective_review_remediation"), dict) else None
            if remediation and str(remediation.get("review_id") or "") == review_id:
                existing_dimensions.add(str(remediation.get("dimension") or ""))
        created: list[str] = []
        for packet in packets:
            verdict = str(packet.get("verdict") or "").strip()
            dimension = str(packet.get("dimension") or "").strip()
            if verdict not in {"concern", "remediation_required"} or not dimension or dimension in existing_dimensions:
                continue
            findings = [str(item).strip() for item in list(packet.get("findings") or []) if str(item).strip()]
            summary = str(packet.get("summary") or "").strip()
            evidence_contract = self.extract_evidence_contract(packet)
            artifact_type = str(evidence_contract.get("required_artifact_type") or "review_artifact")
            title = f"Produce {artifact_type.replace('_', ' ')} for {dimension.replace('_', ' ')} review finding"
            objective_text = self.build_remediation_objective(
                summary=summary,
                findings=findings,
                evidence_contract=evidence_contract,
            )
            task = self.task_service.create_task_with_policy(
                project_id=objective.project_id,
                objective_id=objective.id,
                title=title,
                objective=objective_text,
                priority=objective.priority,
                parent_task_id=None,
                source_run_id=None,
                external_ref_type="objective_review",
                external_ref_id=f"{objective.id}:{review_id}:{dimension}",
                external_ref_metadata={
                    "objective_review_remediation": {
                        "review_id": review_id,
                        "dimension": dimension,
                        "reviewer": str(packet.get("reviewer") or ""),
                        "verdict": verdict,
                        "finding_record_id": str(packet.get("packet_record_id") or ""),
                        "evidence_contract": evidence_contract,
                    }
                },
                validation_profile="generic",
                validation_mode="default_focused",
                scope={},
                strategy="objective_review_remediation",
                max_attempts=3,
                required_artifacts=list(dict.fromkeys(["plan", "report", artifact_type])),
            )
            created.append(task.id)
            existing_dimensions.add(dimension)
        if created:
            self.store.update_objective_phase(objective.id)
        return created

    def extract_evidence_contract(self, packet: dict[str, Any]) -> dict[str, Any]:
        contract = packet.get("evidence_contract") if isinstance(packet.get("evidence_contract"), dict) else {}
        required_artifact_type = str(
            contract.get("required_artifact_type") or packet.get("required_artifact_type") or ""
        ).strip()
        artifact_schema = self.normalize_artifact_schema(
            contract.get("artifact_schema") if contract else packet.get("artifact_schema"),
            required_artifact_type=required_artifact_type,
            dimension=str(packet.get("dimension") or ""),
        ) or {}
        closure_criteria = str(contract.get("closure_criteria") or packet.get("closure_criteria") or "").strip()
        evidence_required = str(contract.get("evidence_required") or packet.get("evidence_required") or "").strip()
        return {
            "required_artifact_type": required_artifact_type,
            "artifact_schema": artifact_schema,
            "closure_criteria": closure_criteria,
            "evidence_required": evidence_required,
        }

    def normalize_artifact_schema(
        self,
        raw: Any,
        *,
        required_artifact_type: str = "",
        dimension: str = "",
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        schema_type = str(raw.get("type") or "").strip() or required_artifact_type or "review_artifact"
        description = str(raw.get("description") or "").strip()
        required_fields = [str(f).strip() for f in list(raw.get("required_fields") or []) if str(f).strip()]
        if not required_fields:
            required_fields = self.default_required_fields(schema_type)
        return {
            "type": schema_type,
            "description": description or f"Artifact for {dimension or 'review'}",
            "required_fields": required_fields,
        }

    @staticmethod
    def default_required_fields(artifact_type: str) -> list[str]:
        lowered = artifact_type.lower()
        if "review_cycle" in lowered or "telemetry" in lowered:
            return ["review_id", "start_event", "packet_persistence_events", "terminal_event", "linked_outcome"]
        if "review_packet" in lowered:
            return ["review_id", "reviewer", "dimension", "verdict", "artifacts"]
        if "test" in lowered:
            return ["artifact_path", "test_targets", "result"]
        if lowered == "failed_task_disposition_record":
            return ["task_id", "disposition", "rationale"]
        return ["artifact_path", "summary"]

    def build_remediation_objective(
        self,
        *,
        summary: str,
        findings: list[str],
        evidence_contract: dict[str, Any],
    ) -> str:
        artifact_type = str(evidence_contract.get("required_artifact_type") or "review_artifact")
        artifact_schema = evidence_contract.get("artifact_schema") if isinstance(evidence_contract.get("artifact_schema"), dict) else {}
        required_fields = [str(item).strip() for item in list(artifact_schema.get("required_fields") or []) if str(item).strip()]
        lines = [
            "A promotion reviewer raised findings that must be addressed before this objective can be promoted.",
            "Read the findings below carefully. They describe concrete problems the reviewer found in the actual codebase.",
            "Your job is to FIX the problems described in the findings — write code, refactor, add tests — whatever the findings require.",
            f"After making the fixes, produce a `{artifact_type}` artifact documenting what you changed and proving the closure criteria are met.",
            "Do NOT fabricate evidence. If the reviewer says a function doesn't exist, you must CREATE it, not write a report claiming it exists.",
        ]
        if summary:
            lines.append(f"Reviewer summary: {summary}")
        if findings:
            lines.append("Findings:")
            lines.extend(f"- {item}" for item in findings)
        if evidence_contract.get("closure_criteria"):
            lines.append(f"Closure criteria: {evidence_contract['closure_criteria']}")
        if evidence_contract.get("evidence_required"):
            lines.append(f"Evidence required: {evidence_contract['evidence_required']}")
        if required_fields:
            lines.append("Artifact schema fields: " + ", ".join(required_fields))
        lines.append("Address the findings FIRST by making real code changes, THEN produce the evidence artifact showing what you did.")
        return "\n".join(lines)

    def classify_rebuttal(
        self,
        packet: dict[str, Any],
        task: Task,
    ) -> str:
        from ..ui_mixins._shared import _OBJECTIVE_REVIEW_REBUTTAL_OUTCOMES
        evidence_contract = self.extract_evidence_contract(packet)
        required_artifact_type = str(evidence_contract.get("required_artifact_type") or "").strip()
        if not required_artifact_type:
            return "accepted"
        runs = self.store.list_runs(task.id)
        completed_runs = [r for r in runs if r.status.value == "completed"]
        if not completed_runs:
            return "missing_terminal_event"
        latest_run = completed_runs[-1]
        artifacts = self.store.list_artifacts(latest_run.id)
        matching = [a for a in artifacts if a.kind == required_artifact_type]
        if not matching:
            return "wrong_artifact_type"
        return "accepted"
