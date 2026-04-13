"""Objective review orchestrator.

Replaces the inline objective-review LLM dance in HarnessUIDataService.
For each of the 7 review dimensions it invokes a single reviewer skill
through invoke_skill and assembles a packets list of the same shape the
existing downstream code expects.

No retry loops. If a reviewer skill fails, the orchestrator records a
stub packet for that dimension with verdict='remediation_required' and a
finding pointing to the error.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain import Run, RunStatus, Task, TaskStatus, new_id
from ..llm import LLMRouter
from ..skills import REVIEWER_SKILLS, SkillInvocation, SkillRegistry, invoke_skill
from ..skills.reviewers.base import BaseReviewerSkill


_DIMENSIONS = (
    "intent_fidelity",
    "unit_test_coverage",
    "integration_e2e_coverage",
    "security",
    "devops",
    "atomic_fidelity",
    "code_structure",
)


def _stub_packet(dimension: str, error_message: str, *, reviewer: str) -> dict[str, Any]:
    """Build a stub packet that downstream remediation code can consume.

    Produces a 'remediation_required' packet whose evidence_contract carries
    the harness's general report artifact type so that downstream remediation
    code does not crash on missing fields.
    """
    closure = (
        f"Reviewer skill '{reviewer}' must produce a valid {dimension} packet for the "
        f"objective. Current failure: {error_message}"
    )
    artifact_schema = {
        "type": "report",
        "description": f"Diagnostic report explaining the {dimension} reviewer failure and a re-review.",
        "required_fields": ["artifact_path", "summary"],
    }
    return {
        "reviewer": reviewer,
        "dimension": dimension,
        "verdict": "remediation_required",
        "progress_status": "new_concern",
        "severity": "medium",
        "owner_scope": "objective review orchestration",
        "summary": f"Reviewer skill failed for dimension {dimension}: {error_message}",
        "findings": [f"Reviewer skill failure: {error_message}"],
        "evidence": [],
        "required_artifact_type": "report",
        "artifact_schema": artifact_schema,
        "evidence_contract": {
            "required_artifact_type": "report",
            "artifact_schema": artifact_schema,
            "closure_criteria": closure,
            "evidence_required": "report artifact summarising the reviewer failure and re-review outcome",
        },
        "closure_criteria": closure,
        "evidence_required": "report artifact summarising the reviewer failure and re-review outcome",
        "repeat_reason": "",
    }


def _packet_from_skill_output(
    skill: BaseReviewerSkill,
    output: dict[str, Any],
) -> dict[str, Any]:
    """Translate a reviewer skill's structured output into the downstream packet shape."""
    verdict = str(output.get("verdict") or "concern").strip().lower()
    summary = str(output.get("summary") or "").strip()
    findings = [str(f).strip() for f in list(output.get("findings") or []) if str(f).strip()]
    evidence = [str(e).strip() for e in list(output.get("evidence") or []) if str(e).strip()]
    severity = str(output.get("severity") or "").strip().lower()
    owner_scope = str(output.get("owner_scope") or "").strip()
    required_artifact_type = str(output.get("required_artifact_type") or "report").strip() or "report"
    closure_criteria = str(output.get("closure_criteria") or "").strip()
    evidence_required = str(output.get("evidence_required") or "").strip()
    artifact_schema_raw = output.get("artifact_schema")
    if isinstance(artifact_schema_raw, dict):
        artifact_schema = dict(artifact_schema_raw)
    else:
        artifact_schema = {
            "type": required_artifact_type,
            "description": f"Artifact for {skill.dimension}",
            "required_fields": ["artifact_path", "summary"],
        }
    if verdict == "pass":
        return {
            "reviewer": skill.reviewer_label,
            "dimension": skill.dimension,
            "verdict": "pass",
            "progress_status": "not_applicable",
            "severity": "",
            "owner_scope": "",
            "summary": summary or f"{skill.dimension} review passed.",
            "findings": findings,
            "evidence": evidence,
            "required_artifact_type": "",
            "artifact_schema": {},
            "evidence_contract": {},
            "closure_criteria": "",
            "evidence_required": "",
            "repeat_reason": "",
        }
    if severity not in {"low", "medium", "high"}:
        severity = "medium"
    if not owner_scope:
        owner_scope = "objective review orchestration"
    if not closure_criteria:
        closure_criteria = (
            f"All findings for the {skill.dimension} dimension must be resolved with concrete evidence."
        )
    if not evidence_required:
        evidence_required = "report artifact recording the resolution of the listed findings"
    if not findings:
        findings = [f"{skill.dimension} reviewer flagged a {verdict} verdict without explicit findings."]
    evidence_contract = {
        "required_artifact_type": required_artifact_type,
        "artifact_schema": artifact_schema,
        "closure_criteria": closure_criteria,
        "evidence_required": evidence_required,
    }
    return {
        "reviewer": skill.reviewer_label,
        "dimension": skill.dimension,
        "verdict": verdict if verdict in {"concern", "remediation_required"} else "concern",
        "progress_status": "new_concern",
        "severity": severity,
        "owner_scope": owner_scope,
        "summary": summary or f"{skill.dimension} reviewer raised a finding.",
        "findings": findings,
        "evidence": evidence,
        "required_artifact_type": required_artifact_type,
        "artifact_schema": artifact_schema,
        "evidence_contract": evidence_contract,
        "closure_criteria": closure_criteria,
        "evidence_required": evidence_required,
        "repeat_reason": "",
    }


class ObjectiveReviewOrchestrator:
    """Drives the 7 reviewer skills for a single objective review run."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        llm_router: LLMRouter,
        store: Any,
        workspace_root: Path,
        telemetry: Any = None,
    ) -> None:
        self.skill_registry = skill_registry
        self.llm_router = llm_router
        self.store = store
        self.workspace_root = Path(workspace_root)
        self.telemetry = telemetry

    def execute(self, objective_id: str, review_id: str) -> dict[str, Any]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return {"packets": [], "review_clear": False, "failed_count": 0}
        intent_model = self.store.latest_intent_model(objective_id)
        mermaid = None
        if hasattr(self.store, "latest_mermaid_artifact"):
            try:
                mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
            except Exception:
                mermaid = None
        linked_tasks = [
            task
            for task in self.store.list_tasks(objective.project_id)
            if getattr(task, "objective_id", None) == objective_id
        ]
        task_titles = [t.title for t in linked_tasks]
        inputs: dict[str, Any] = {
            "objective_title": objective.title,
            "objective_summary": objective.summary,
            "intent_summary": intent_model.intent_summary if intent_model else "",
            "success_definition": intent_model.success_definition if intent_model else "",
            "non_negotiables": list(intent_model.non_negotiables) if intent_model else [],
            "mermaid_content": mermaid.content if mermaid else "",
            "task_titles": task_titles,
            "changed_files": [],
            "diff_text": "",
        }

        run_dir_root = self.workspace_root / "objective_review_orchestrator" / objective_id / review_id
        run_dir_root.mkdir(parents=True, exist_ok=True)

        packets: list[dict[str, Any]] = []
        failed_count = 0

        # Telemetry: total span
        total_span = None
        if self.telemetry is not None and hasattr(self.telemetry, "timed"):
            total_span = self.telemetry.timed(
                "skills_objective_review",
                objective_id=objective_id,
                review_id=review_id,
            )
            total_span.__enter__()

        try:
            for dimension in _DIMENSIONS:
                skill_name = f"review_{dimension}"
                try:
                    skill = self.skill_registry.get(skill_name)
                except KeyError as exc:
                    failed_count += 1
                    packets.append(_stub_packet(dimension, f"skill_not_registered: {exc}", reviewer=skill_name))
                    continue

                run_dir = run_dir_root / dimension
                run_dir.mkdir(parents=True, exist_ok=True)
                task = Task(
                    id=new_id(f"objreview_{dimension}_task"),
                    project_id=objective.project_id,
                    title=f"Objective review {dimension} for {objective.title}",
                    objective=f"Generate a {dimension} review packet.",
                    strategy="objective_review",
                    status=TaskStatus.COMPLETED,
                )
                run = Run(
                    id=new_id(f"objreview_{dimension}_run"),
                    task_id=task.id,
                    status=RunStatus.COMPLETED,
                    attempt=1,
                    summary=f"Objective review for {dimension}",
                )
                invocation = SkillInvocation(
                    skill_name=skill_name,
                    inputs=inputs,
                    task=task,
                    run=run,
                    run_dir=run_dir,
                )

                dim_span = None
                if self.telemetry is not None and hasattr(self.telemetry, "timed"):
                    dim_span = self.telemetry.timed(
                        f"skills_review_{dimension}",
                        objective_id=objective_id,
                        review_id=review_id,
                    )
                    dim_span.__enter__()
                try:
                    skill_result = invoke_skill(
                        skill,
                        invocation,
                        self.llm_router,
                        telemetry=self.telemetry,
                    )
                finally:
                    if dim_span is not None:
                        dim_span.__exit__(None, None, None)

                if not skill_result.success:
                    failed_count += 1
                    error_message = "; ".join(skill_result.errors) or "unknown error"
                    packets.append(_stub_packet(dimension, error_message, reviewer=skill.reviewer_label))
                    continue
                packets.append(_packet_from_skill_output(skill, skill_result.output))
        finally:
            if total_span is not None:
                total_span.__exit__(None, None, None)

        review_clear = failed_count == 0 and all(
            str(p.get("verdict") or "") == "pass" for p in packets
        )
        return {
            "packets": packets,
            "review_clear": review_clear,
            "failed_count": failed_count,
        }
