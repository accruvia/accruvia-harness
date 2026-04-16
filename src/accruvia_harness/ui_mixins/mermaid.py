"""HarnessUIDataService mermaid diagram methods."""
from __future__ import annotations

import json
import re
from typing import Any

from ..domain import (
    ContextRecord, MermaidArtifact, MermaidStatus, Objective,
    ObjectivePhase, ObjectiveStatus, Run, Task, new_id, serialize_dataclass,
)
from ._shared import _MERMAID_RED_TEAM_MAX_ROUNDS

from ._shared import _mermaid_node_id_for_task

class MermaidMixin:

    def update_mermaid_artifact(
        self,
        objective_id: str,
        *,
        status: str,
        summary: str,
        blocking_reason: str,
        author_type: str = "operator",
        async_generation: bool = True,
    ) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        if getattr(self.ctx, "is_test", False):
            async_generation = False
        normalized = status.strip().lower()
        try:
            next_status = MermaidStatus(normalized)
        except ValueError as exc:
            raise ValueError(f"Unsupported Mermaid status: {status}") from exc

        latest = self.store.latest_mermaid_artifact(objective.id, "workflow_control")
        if latest is None:
            latest = self._create_seed_mermaid(objective)
        content = latest.content if latest is not None else self._default_objective_mermaid(objective)
        artifact = MermaidArtifact(
            id=new_id("diagram"),
            objective_id=objective.id,
            diagram_type="workflow_control",
            version=self.store.next_mermaid_version(objective.id, "workflow_control"),
            status=next_status,
            summary=(summary.strip() or latest.summary or f"{next_status.value} workflow review"),
            content=content,
            required_for_execution=True,
            blocking_reason=blocking_reason.strip(),
            author_type=author_type,
        )
        self.store.create_mermaid_artifact(artifact)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_status_change",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type=author_type,
                content=f"Mermaid workflow_control marked {next_status.value}",
                metadata={
                    "diagram_id": artifact.id,
                    "diagram_type": artifact.diagram_type,
                    "version": artifact.version,
                    "status": artifact.status.value,
                    "required_for_execution": artifact.required_for_execution,
                    "blocking_reason": artifact.blocking_reason,
                },
            )
        )
        if next_status == MermaidStatus.PAUSED:
            self.store.update_objective_status(objective.id, ObjectiveStatus.PAUSED)
        elif next_status == MermaidStatus.FINISHED:
            runner = self.start_objective_lifecycle(objective.id)
            # Ensure phase is at MERMAID_REVIEW before approving.
            # For objectives that haven't been through the runner yet,
            # fast-forward to MERMAID_REVIEW.
            if runner.phase == ObjectivePhase.CREATED:
                runner._advance(ObjectivePhase.INTERROGATING)
                runner._advance(ObjectivePhase.MERMAID_REVIEW)
            runner.approve_mermaid()
            self.store.update_objective_status(objective.id, ObjectiveStatus.PLANNING)
            self.complete_interrogation_review(objective.id)
            self.queue_atomic_generation(objective.id, async_mode=async_generation, runner=runner)
        else:
            self.store.update_objective_status(objective.id, ObjectiveStatus.INVESTIGATING)
        self.reconcile_objective_workflow(objective.id)
        return {"diagram": serialize_dataclass(artifact)}


    def propose_mermaid_update(self, objective_id: str, *, directive: str) -> dict[str, object] | None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        proposal = self._generate_mermaid_update_proposal(objective_id, directive=directive)
        if proposal is None:
            return None
        record = ContextRecord(
            id=new_id("context"),
            record_type="mermaid_update_proposed",
            project_id=objective.project_id,
            objective_id=objective.id,
            visibility="model_visible",
            author_type="system",
            content=proposal["summary"],
            metadata={
                "content": proposal["content"],
                "summary": proposal["summary"],
                "directive": directive,
                "backend": proposal.get("backend", ""),
                "prompt_path": proposal.get("prompt_path", ""),
                "response_path": proposal.get("response_path", ""),
                "red_team_review": proposal.get("red_team_review", ""),
            },
        )
        self.store.create_context_record(record)
        return {
            "id": record.id,
            "summary": record.content,
            "content": str(record.metadata.get("content") or ""),
            "directive": directive,
            "created_at": record.created_at.isoformat(),
        }


    def accept_mermaid_proposal(self, objective_id: str, proposal_id: str, *, async_generation: bool = True) -> dict[str, object]:
        if getattr(self.ctx, "is_test", False):
            async_generation = False
        objective = self.store.get_objective(objective_id)
        proposal = self._proposal_record(objective_id, proposal_id)
        if objective is None or proposal is None:
            raise ValueError("Unknown Mermaid proposal")
        content = str(proposal.metadata.get("content") or "").strip()
        if not content:
            raise ValueError("Mermaid proposal content is empty")
        artifact = MermaidArtifact(
            id=new_id("diagram"),
            objective_id=objective.id,
            diagram_type="workflow_control",
            version=self.store.next_mermaid_version(objective.id, "workflow_control"),
            status=MermaidStatus.FINISHED,
            summary=str(proposal.metadata.get("summary") or proposal.content or "Accepted control flow"),
            content=content,
            required_for_execution=True,
            blocking_reason="",
            author_type="operator",
        )
        self.store.create_mermaid_artifact(artifact)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_status_change",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="operator",
                content="Mermaid workflow_control marked finished",
                metadata={
                    "diagram_id": artifact.id,
                    "diagram_type": artifact.diagram_type,
                    "version": artifact.version,
                    "status": artifact.status.value,
                    "required_for_execution": artifact.required_for_execution,
                    "blocking_reason": artifact.blocking_reason,
                },
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_update_accepted",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="operator",
                content="Accepted proposed Mermaid update.",
                metadata={"proposal_id": proposal.id, "diagram_id": artifact.id, "version": artifact.version},
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Action receipt: Exact proposal on screen promoted unchanged to locked current version {artifact.version}. No regeneration occurred.",
                metadata={
                    "kind": "mermaid_update",
                    "status": "accepted",
                    "proposal_id": proposal.id,
                    "diagram_id": artifact.id,
                    "promotion_mode": "exact_proposal",
                },
            )
        )
        self.store.update_objective_status(objective.id, ObjectiveStatus.PLANNING)
        self.queue_atomic_generation(objective.id, async_mode=async_generation)
        self.reconcile_objective_workflow(objective.id)
        return {"diagram": serialize_dataclass(artifact)}


    def reject_mermaid_proposal(self, objective_id: str, proposal_id: str, *, resolution: str = "refine") -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        proposal = self._proposal_record(objective_id, proposal_id)
        if objective is None or proposal is None:
            raise ValueError("Unknown Mermaid proposal")
        normalized = resolution.strip().lower() or "refine"
        if normalized not in {"refine", "rewind_hard"}:
            raise ValueError(f"Unsupported Mermaid proposal resolution: {resolution}")
        record_type = "mermaid_update_rejected" if normalized == "refine" else "mermaid_update_rewound"
        content = "Keep refining the Mermaid update." if normalized == "refine" else "Rewind the Mermaid update and reconsider from the last approved diagram."
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type=record_type,
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="operator",
                content=content,
                metadata={"proposal_id": proposal.id, "resolution": normalized},
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=(
                    "Action receipt: Mermaid proposal kept for further refinement."
                    if normalized == "refine"
                    else "Action receipt: Mermaid proposal rewound hard to the last approved diagram."
                ),
                metadata={"kind": "mermaid_update", "status": normalized, "proposal_id": proposal.id},
            )
        )
        self.reconcile_objective_workflow(objective.id)
        return {"rejected": True, "proposal_id": proposal.id, "resolution": normalized}


    def _generate_mermaid_update_proposal(self, objective_id: str, *, directive: str) -> dict[str, str] | None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return None
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is None or not getattr(llm_router, "executors", {}):
            return None
        intent_model = self.store.latest_intent_model(objective_id)
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        comments = self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")[-12:]
        anchor_match = re.search(r"\[Mermaid anchor:\s*([^\]]+)\]", directive)
        anchor_label = anchor_match.group(1).strip() if anchor_match else ""
        rewrite_requested = bool(
            re.search(r"\b(rewrite|regenerate|redo|rebuild|start over|restructure|replace the diagram|full rewrite)\b", directive, flags=re.IGNORECASE)
        )
        orchestrator = self._red_team_loop_orchestrator(llm_router)
        initial_inputs = {
            "objective_title": objective.title,
            "objective_summary": objective.summary,
            "intent_summary": intent_model.intent_summary if intent_model else "",
            "success_definition": intent_model.success_definition if intent_model else "",
            "non_negotiables": list(intent_model.non_negotiables) if intent_model else [],
            "current_mermaid": mermaid.content if mermaid else "",
            "directive": directive,
            "anchor_label": anchor_label,
            "rewrite_requested": rewrite_requested,
            "recent_comments": [r.content for r in comments],
        }
        latest_review_box: dict[str, object] = {"review": None}

        def run_mermaid_review(proposed_text: str) -> dict[str, object]:
            try:
                return interrogation_service.red_team_mermaid_text(
                    proposed_text,
                    source_label=f"mermaid_proposal_{objective_id}",
                    include_llm=False,
                )
            except Exception as exc:  # noqa: BLE001
                return {
                    "ready_for_human_review": False,
                    "deterministic_review": {"findings": [
                        {"severity": "critical", "message": f"mermaid review failed: {exc}"}
                    ]},
                    "llm_review": {"findings": []},
                }

        def stopping_predicate(output, reviewer_results, round_number):
            proposed = str(output.get("proposed_content") or "")
            if not proposed:
                return True  # bail — nothing to review, let loop record failure
            review = run_mermaid_review(proposed)
            latest_review_box["review"] = review
            deterministic_findings = list((review.get("deterministic_review") or {}).get("findings") or [])
            major = [
                f for f in deterministic_findings
                if str(f.get("severity") or "").lower() in {"critical", "major"}
            ]
            return bool(review.get("ready_for_human_review")) and not major

        def findings_extractor(generator_output, reviewer_results):
            review = latest_review_box.get("review") or {}
            findings: list[str] = []
            for item in list((review.get("deterministic_review") or {}).get("findings") or []):
                summary = str(item.get("summary") or item.get("message") or item.get("finding") or "").strip()
                patch_hint = str(item.get("patch_hint") or "").strip()
                severity = str(item.get("severity") or "info").strip()
                if summary or patch_hint:
                    line = f"[deterministic:{severity}] {summary}"
                    if patch_hint:
                        line += f" — fix: {patch_hint}"
                    findings.append(line)
            return findings

        loop_result = orchestrator.execute(
            generator_skill_name="mermaid_update_proposal",
            reviewer_skill_names=None,
            initial_inputs=initial_inputs,
            stopping_predicate=stopping_predicate,
            max_rounds=_MERMAID_RED_TEAM_MAX_ROUNDS,
            project_id=objective.project_id,
            loop_label="mermaid_update_proposal",
            loop_key=objective.id,
            findings_extractor=findings_extractor,
        )
        if not loop_result.success or not loop_result.final_output:
            return None
        proposed_content = str(loop_result.final_output.get("proposed_content") or "")
        rationale = str(loop_result.final_output.get("rationale") or "")
        if not proposed_content:
            return None
        last_round = loop_result.history[-1] if loop_result.history else None
        last_review = latest_review_box.get("review") or {}
        return {
            "summary": rationale,
            "content": proposed_content,
            "backend": last_round.generator_result.llm_backend if last_round else "",
            "prompt_path": last_round.generator_result.prompt_path if last_round else "",
            "response_path": last_round.generator_result.response_path if last_round else "",
            "red_team_rounds": loop_result.rounds_completed,
            "red_team_stop_reason": loop_result.stop_reason,
            "red_team_review": json.dumps(last_review, indent=2, sort_keys=True),
        }


    def _parse_mermaid_update_response(self, text: str) -> dict[str, str] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        candidates.extend(fenced)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            summary = str(payload.get("summary") or "").strip()
            content = str(payload.get("content") or "").strip()
            if summary and content:
                return {"summary": summary, "content": content}
        return None


    def _proposal_record(self, objective_id: str, proposal_id: str) -> ContextRecord | None:
        for record in self.store.list_context_records(objective_id=objective_id, record_type="mermaid_update_proposed"):
            if record.id == proposal_id:
                return record
        return None


    def _latest_mermaid_proposal(self, objective_id: str) -> dict[str, object] | None:
        proposals = self.store.list_context_records(objective_id=objective_id, record_type="mermaid_update_proposed")
        if not proposals:
            return None
        resolutions = {
            str(record.metadata.get("proposal_id") or "")
            for record in self.store.list_context_records(objective_id=objective_id)
            if record.record_type in {"mermaid_update_accepted", "mermaid_update_rejected", "mermaid_update_rewound"}
        }
        proposal = proposals[-1]
        if proposal.id in resolutions:
            return None
        return {
            "id": proposal.id,
            "summary": proposal.content,
            "content": str(proposal.metadata.get("content") or ""),
            "directive": str(proposal.metadata.get("directive") or ""),
            "backend": str(proposal.metadata.get("backend") or ""),
            "created_at": proposal.created_at.isoformat(),
        }


    def _create_seed_mermaid(self, objective: Objective) -> MermaidArtifact:
        artifact = MermaidArtifact(
            id=new_id("diagram"),
            objective_id=objective.id,
            diagram_type="workflow_control",
            version=self.store.next_mermaid_version(objective.id, "workflow_control"),
            status=MermaidStatus.DRAFT,
            summary="Initial workflow draft",
            content=self._default_objective_mermaid(objective),
            required_for_execution=True,
            blocking_reason="Workflow review has not been completed yet.",
            author_type="system",
        )
        self.store.create_mermaid_artifact(artifact)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_seeded",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="system",
                content="Seeded initial required Mermaid workflow.",
                metadata={
                    "diagram_id": artifact.id,
                    "diagram_type": artifact.diagram_type,
                    "version": artifact.version,
                    "status": artifact.status.value,
                },
            )
        )
        return artifact


    def _default_objective_mermaid(self, objective: Objective) -> str:
        """Generate an objective decomposition diagram from the plan set.

        Delegates to `mermaid.render_mermaid_from_plans`, the single canonical
        renderer. Plans are the source of truth; node IDs are `P_<plan_hash>`
        (stable across revisions). Falls back to the "awaiting decomposition"
        placeholder when no plans exist yet.

        The previous implementation rendered from tasks using
        `_mermaid_node_id_for_task(task.id)` which produced `T_<task_suffix>`
        IDs. That path is removed — task IDs and plan IDs are no longer
        conflated. See Query #3 findings + the canonical ID design notes.
        """
        from ..mermaid import render_mermaid_from_plans
        plans = self.store.list_plans_for_objective(objective.id)
        return render_mermaid_from_plans(plans, objective)


    @staticmethod
    def _mermaid_label(value: str) -> str:
        return value.replace('"', "'")


    def _project_mermaid(self, project_id: str, tasks, runs_by_task: dict[str, list[Any]]) -> str:
        project = self.store.get_project(project_id)
        title = project.name if project is not None else project_id
        lines = ["flowchart TD", f'    P["Project: {self._mermaid_label(title)}"]']
        sorted_tasks = sorted(tasks, key=lambda item: (item.created_at, item.priority, item.id))
        latest_run_ids: list[str] = []
        for index, task in enumerate(sorted_tasks, start=1):
            task_node = f"T{index}"
            task_label = f"Task: {task.title}\\n{task.status.value} · {task.strategy}"
            lines.append(f'    {task_node}["{self._mermaid_label(task_label)}"]')
            if task.parent_task_id:
                parent_index = next(
                    (i for i, candidate in enumerate(sorted_tasks, start=1) if candidate.id == task.parent_task_id),
                    None,
                )
                if parent_index is not None:
                    lines.append(f"    T{parent_index} --> {task_node}")
                else:
                    lines.append(f"    P --> {task_node}")
            else:
                lines.append(f"    P --> {task_node}")
            runs = runs_by_task.get(task.id, [])
            if runs:
                latest_run = runs[-1]
                latest_run_ids.append(latest_run.id)
                run_node = f"R{index}"
                run_label = f"Run {latest_run.attempt}\\n{latest_run.status.value}"
                lines.append(f'    {run_node}["{self._mermaid_label(run_label)}"]')
                lines.append(f"    {task_node} --> {run_node}")
        if not sorted_tasks:
            lines.append('    P --> I["No tasks yet"]')
        return "\n".join(lines)

