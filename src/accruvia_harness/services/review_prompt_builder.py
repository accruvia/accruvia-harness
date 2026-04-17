"""Prompt building, response parsing, and packet validation for objective review."""
from __future__ import annotations

import json
import re
from typing import Any

from ..domain import ContextRecord, ReviewPacket, ReviewVerdict, new_id
from ..ui_mixins._shared import (
    _OBJECTIVE_REVIEW_DIMENSIONS,
    _OBJECTIVE_REVIEW_PROGRESS,
    _OBJECTIVE_REVIEW_VERDICTS,
    _OBJECTIVE_REVIEW_SEVERITIES,
    _OBJECTIVE_REVIEW_VAGUE_PHRASES,
)


class ReviewPromptBuilder:
    """Extracted from ObjectiveReviewMixin."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def _build_objective_review_prompt(
        self,
        objective: Objective,
        objective_payload: dict[str, object],
        linked_tasks: list[Task],
    ) -> str:
        intent_model = self.store.latest_intent_model(objective.id)
        tasks_payload = [
            {
                "title": task.title,
                "status": task.status.value,
                "objective": task.objective,
                "strategy": task.strategy,
                "metadata": task.external_ref_metadata,
            }
            for task in linked_tasks
        ]
        prior_rounds = []
        for round_row in list(objective_payload.get("review_rounds") or [])[:3]:
            if not isinstance(round_row, dict):
                continue
            prior_rounds.append(
                {
                    "round_number": round_row.get("round_number"),
                    "status": round_row.get("status"),
                    "verdict_counts": round_row.get("verdict_counts"),
                    "remediation_counts": round_row.get("remediation_counts"),
                    "review_cycle_artifact": round_row.get("review_cycle_artifact"),
                    "worker_responses": round_row.get("worker_responses"),
                    "reviewer_rebuttals": round_row.get("reviewer_rebuttals"),
                    "packets": [
                        {
                            "dimension": packet.get("dimension"),
                            "verdict": packet.get("verdict"),
                            "progress_status": packet.get("progress_status"),
                            "summary": packet.get("summary"),
                            "evidence_contract": packet.get("evidence_contract"),
                        }
                        for packet in list(round_row.get("packets") or [])
                    ],
                }
            )
        return (
            "You are the objective-level promotion review board for the accruvia harness.\n"
            "Review the objective as a whole after execution completed.\n"
            "You may be reviewing a later round after remediation from prior rounds.\n"
            "Judge progress against previous rounds instead of repeating the same concern blindly.\n"
            "Every non-pass packet becomes an Evidence Contract for remediation. Review findings and remediation must speak the same artifact type.\n"
            "Do not treat an actively running review/remediation cycle as proof of failure on its own.\n"
            "If the current lifecycle is still in progress, distinguish missing implementation from missing final evidence.\n"
            "Return JSON only with keys: summary, packets.\n"
            "packets must be an array with EXACTLY 7 packets — one for each dimension listed below. Every dimension MUST appear. If a dimension has no findings, return it with verdict pass.\n"
            "Each packet must contain reviewer, dimension, verdict, progress_status, severity, owner_scope, summary, findings, evidence, required_artifact_type, artifact_schema, closure_criteria, evidence_required.\n"
            "reviewer: short reviewer name\n"
            "dimension: REQUIRED dimensions (all 7 must appear): intent_fidelity, unit_test_coverage, integration_e2e_coverage, security, devops, atomic_fidelity, code_structure\n"
            "verdict: one of pass, concern, remediation_required\n"
            "progress_status: one of new_concern, still_blocking, improving, resolved, not_applicable\n"
            "severity: one of low, medium, high\n"
            "owner_scope: short concrete owner scope such as objective review orchestration, integration tests, promotion apply-back, ui workflow\n"
            "summary: short paragraph\n"
            "findings: array of short strings\n"
            "evidence: array of short strings\n"
            "required_artifact_type: REQUIRED for concern and remediation_required. Must be one of the artifact types the harness can actually produce: "
            "plan, report, test_execution_report, ui_workflow_test_report, ui_workflow_e2e_trace_report, "
            "stale_recovery_test_evidence, completed_task_reconciliation_report, workflow_implementation_evidence_bundle. "
            "Do NOT invent artifact types that are not in this list — the remediation worker can only produce these types.\n"
            "artifact_schema: REQUIRED for concern and remediation_required. JSON object with at least type, description, and required_fields.\n"
            "closure_criteria: REQUIRED for concern and remediation_required. Must be concrete and measurable.\n"
            "evidence_required: REQUIRED for concern and remediation_required. Must name the artifact or proof required to clear the finding.\n"
            "repeat_reason: REQUIRED when verdict is concern or remediation_required and progress_status is improving, still_blocking, or resolved.\n"
            "Reject vague language. Do not say 'improve testing' or 'more evidence' without a measurable closure target.\n"
            "\n"
            "CONVERSATION RULES — this is a dialogue, not a monologue:\n"
            "Previous review rounds include worker_responses from remediation tasks. These are the worker's replies to your evidence contracts. Read them carefully.\n"
            "If a worker produced the artifact you asked for, check whether it satisfies your closure criteria. If it does, mark the dimension resolved/pass.\n"
            "If a worker produced a DIFFERENT artifact type than you demanded, read their response as pushback — they may be telling you the demanded type is not achievable. "
            "Consider whether the substitute artifact adequately demonstrates the same concern is addressed. If so, accept it and move to pass.\n"
            "If a worker completed the task but produced no matching artifact, treat that as the worker saying the demand is infeasible. "
            "Revise your required_artifact_type to something from the producible list above, or accept existing evidence and move to pass.\n"
            "\n"
            "SELF-DOUBT WHEN REPEATING — each round the worker returns the same response to your demand, the probability that YOU are wrong increases:\n"
            "Round 1: State your concern clearly with a concrete evidence contract.\n"
            "Round 2 (same response): The worker may not understand. Rephrase your demand more precisely. Confirm the artifact type exists in the producible list.\n"
            "Round 3+ (same response): You are likely hallucinating a requirement or asking something impossible. "
            "Before repeating still_blocking, search the codebase context for evidence that your demand is achievable. "
            "Include in your repeat_reason a specific, factual argument citing code paths, test files, or artifact schemas that prove your demand is reasonable. "
            "If you cannot make that factual case, you are wrong — revise your demand or accept the worker's evidence and move to pass.\n"
            "The burden of proof shifts to YOU with each repeated round. Argue with facts, not assertions.\n\n"
            f"Objective title: {objective.title}\n"
            f"Objective summary: {objective.summary}\n"
            f"Objective status: {objective.status.value}\n"
            f"Intent summary: {intent_model.intent_summary if intent_model else ''}\n"
            f"Success definition: {intent_model.success_definition if intent_model else ''}\n"
            f"Objective review summary: {json.dumps(objective_payload, indent=2, sort_keys=True)}\n"
            f"Previous review rounds: {json.dumps(prior_rounds, indent=2, sort_keys=True)}\n"
            f"Linked tasks: {json.dumps(tasks_payload, indent=2, sort_keys=True)}\n"
        )


    def _parse_objective_review_response(
        self,
        text: str,
        *,
        objective_payload: dict[str, object] | None = None,
    ) -> list[dict[str, object]] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        candidates.extend(fenced)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            packets = payload.get("packets")
            if not isinstance(packets, list):
                continue
            parsed: list[dict[str, object]] = []
            for item in packets:
                if not isinstance(item, dict):
                    continue
                validated = self._validate_objective_review_packet(item, objective_payload=objective_payload)
                if validated is not None:
                    parsed.append(validated)
            if parsed:
                return parsed
        return None


    def _validate_objective_review_packet(
        self,
        item: dict[str, object],
        *,
        objective_payload: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        reviewer = str(item.get("reviewer") or "").strip()
        dimension = str(item.get("dimension") or "").strip()
        verdict = str(item.get("verdict") or "").strip()
        progress_status = str(item.get("progress_status") or "not_applicable").strip() or "not_applicable"
        summary = str(item.get("summary") or "").strip()
        findings = [str(v).strip() for v in list(item.get("findings") or []) if str(v).strip()]
        evidence = [str(v).strip() for v in list(item.get("evidence") or []) if str(v).strip()]
        severity = str(item.get("severity") or "").strip().lower()
        owner_scope = str(item.get("owner_scope") or "").strip()
        contract_payload = item.get("evidence_contract") if isinstance(item.get("evidence_contract"), dict) else {}
        required_artifact_type = str(
            item.get("required_artifact_type") or contract_payload.get("required_artifact_type") or ""
        ).strip()
        from .remediation_service import RemediationService
        artifact_schema = RemediationService(self.store, None).normalize_artifact_schema(
            item.get("artifact_schema") if item.get("artifact_schema") is not None else contract_payload.get("artifact_schema"),
            required_artifact_type=required_artifact_type,
            dimension=dimension,
        )
        closure_criteria = str(item.get("closure_criteria") or contract_payload.get("closure_criteria") or "").strip()
        evidence_required = str(item.get("evidence_required") or contract_payload.get("evidence_required") or "").strip()
        repeat_reason = str(item.get("repeat_reason") or "").strip()
        if not reviewer or not summary:
            return None
        if dimension not in _OBJECTIVE_REVIEW_DIMENSIONS:
            return None
        if verdict not in _OBJECTIVE_REVIEW_VERDICTS:
            return None
        if progress_status not in _OBJECTIVE_REVIEW_PROGRESS:
            return None
        if verdict == "pass":
            return {
                "reviewer": reviewer,
                "dimension": dimension,
                "verdict": verdict,
                "progress_status": progress_status,
                "severity": "",
                "owner_scope": "",
                "summary": summary,
                "findings": findings,
                "evidence": evidence,
                "required_artifact_type": "",
                "artifact_schema": {},
                "evidence_contract": {},
                "closure_criteria": "",
                "evidence_required": "",
                "repeat_reason": repeat_reason,
            }
        if severity not in _OBJECTIVE_REVIEW_SEVERITIES:
            return None
        if not owner_scope or not closure_criteria or not evidence_required or not required_artifact_type or artifact_schema is None:
            return None
        if progress_status in {"improving", "still_blocking", "resolved"} and not repeat_reason:
            return None
        if not findings or not evidence:
            return None
        lowered_closure = closure_criteria.lower()
        lowered_evidence_required = evidence_required.lower()
        if not any(
            marker in lowered_closure
            for marker in ("must", "shows", "show", "recorded", "exists", "complete", "passes", "pass", "zero", "all ", "at least", "no ")
        ):
            return None
        if any(phrase in lowered_closure for phrase in _OBJECTIVE_REVIEW_VAGUE_PHRASES):
            return None
        if any(phrase in lowered_evidence_required for phrase in ("more evidence", "stronger evidence", "better tests", "improve")):
            return None
        if (
            objective_payload
            and progress_status in {"improving", "still_blocking", "resolved"}
            and self._objective_round_artifact_is_present(objective_payload)
            and self._packet_requests_round_artifact(evidence_required, closure_criteria)
        ):
            return None
        evidence_contract = {
            "required_artifact_type": required_artifact_type,
            "artifact_schema": artifact_schema,
            "closure_criteria": closure_criteria,
            "evidence_required": evidence_required,
        }
        return {
            "reviewer": reviewer,
            "dimension": dimension,
            "verdict": verdict,
            "progress_status": progress_status,
            "severity": severity,
            "owner_scope": owner_scope,
            "summary": summary,
            "findings": findings,
            "evidence": evidence,
            "required_artifact_type": required_artifact_type,
            "artifact_schema": artifact_schema,
            "evidence_contract": evidence_contract,
            "closure_criteria": closure_criteria,
            "evidence_required": evidence_required,
            "repeat_reason": repeat_reason,
        }


    def _objective_round_artifact_is_present(self, objective_payload: dict[str, object]) -> bool:
        rounds = list(objective_payload.get("review_rounds") or [])
        if not rounds:
            return False
        latest = rounds[0] if isinstance(rounds[0], dict) else {}
        if not latest:
            return False
        cycle_artifact = latest.get("review_cycle_artifact") if isinstance(latest.get("review_cycle_artifact"), dict) else {}
        if cycle_artifact:
            return bool(cycle_artifact.get("record_id")) and bool(cycle_artifact.get("terminal_event"))
        packet_count = int(latest.get("packet_count") or 0)
        completed_at = str(latest.get("completed_at") or "")
        verdict_counts = latest.get("verdict_counts") if isinstance(latest.get("verdict_counts"), dict) else {}
        remediation_counts = latest.get("remediation_counts") if isinstance(latest.get("remediation_counts"), dict) else {}
        terminal_branch_present = (
            str(latest.get("status") or "") == "passed"
            or int(remediation_counts.get("total", 0) or 0) > 0
        )
        return bool(completed_at) and packet_count >= 7 and sum(int(verdict_counts.get(k, 0) or 0) for k in ("pass", "concern", "remediation_required")) > 0 and terminal_branch_present


    def _packet_requests_round_artifact(self, evidence_required: str, closure_criteria: str) -> bool:
        text = f"{evidence_required}\n{closure_criteria}".lower()
        markers = (
            "completed objective review",
            "completed objective review run artifact",
            "persisted objective review artifact",
            "completed end-to-end objective review",
            "completed objective review cycle",
            "completed round",
            "terminal round state",
            "completed_at",
            "persisted reviewer packets",
            "review start",
            "terminal review",
            "review approval",
            "remediation linkage",
        )
        return any(marker in text for marker in markers)

