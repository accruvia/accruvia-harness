"""HarnessUIDataService operator interaction methods."""
from __future__ import annotations

import datetime as _dt
import threading
from typing import Any

from ..commands.common import resolve_project_ref
from ..domain import (
    ContextRecord, MermaidStatus, ObjectiveStatus, Run, Task, new_id, serialize_dataclass,
)

from ._shared import ResponderResult

from ..frustration_triage import triage_frustration

class OperatorMixin:

    def add_operator_comment(
        self,
        project_ref: str,
        text: str,
        author: str | None,
        objective_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        body = text.strip()
        if not body:
            raise ValueError("Comment text must not be empty")
        if objective_id:
            objective = self.store.get_objective(objective_id)
            if objective is None or objective.project_id != project.id:
                raise ValueError(f"Unknown objective: {objective_id}")
        selected_task = None
        if task_id:
            selected_task = self.store.get_task(task_id)
            if selected_task is None or selected_task.project_id != project.id:
                raise ValueError(f"Unknown task: {task_id}")
            if objective_id and selected_task.objective_id != objective_id:
                raise ValueError(f"Task {task_id} does not belong to objective {objective_id}")
            if objective_id is None:
                objective_id = selected_task.objective_id
        record = self.context_recorder.record_operator_comment(
            project_id=project.id,
            objective_id=objective_id,
            task_id=task_id,
            author=author,
            content=body,
        )
        if task_id:
            return self._enqueue_task_question(
                project_id=project.id,
                objective_id=objective_id,
                task_id=task_id,
                comment_record=record,
                frustration_detected=self._comment_looks_like_frustration(body),
            )
        if objective_id and self._should_auto_complete_interrogation(objective_id):
            self.complete_interrogation_review(objective_id)
        frustration_detected = self._comment_looks_like_frustration(body)
        mermaid_update_requested = False
        if objective_id:
            mermaid_update_requested = self._comment_requests_mermaid_update(
                body,
                project_id=project.id,
                objective_id=objective_id,
            )
        responder_result = self._answer_operator_comment(
            project_id=project.id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=body,
            frustration_detected=frustration_detected,
        )
        proposal = None
        if objective_id and mermaid_update_requested:
            responder_result.reply = (
                responder_result.reply.rstrip()
                + "\n\nGenerating a proposed Mermaid update from your instruction — this will appear shortly."
            )
            responder_result.recommended_action = "review_mermaid"
            _proposal_objective_id = objective_id
            _proposal_project_id = project.id
            _proposal_directive = body

            def _generate_proposal() -> dict[str, object] | None:
                result = self.propose_mermaid_update(_proposal_objective_id, directive=_proposal_directive)
                receipt_content = (
                    "Action receipt: Mermaid proposal generated."
                    if result is not None
                    else "Action receipt: Mermaid update was requested but no proposal was generated."
                )
                receipt_status = "proposal_generated" if result is not None else "not_applied"
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="action_receipt",
                        project_id=_proposal_project_id,
                        objective_id=_proposal_objective_id,
                        visibility="operator_visible",
                        author_type="system",
                        content=receipt_content,
                        metadata={"kind": "mermaid_update", "status": receipt_status},
                    )
                )
                return result

            if getattr(self.ctx, "is_test", False):
                try:
                    proposal = _generate_proposal()
                except Exception:
                    proposal = None
            else:
                # Run Mermaid proposal in background so the text reply returns immediately.
                def _generate_proposal_background() -> None:
                    try:
                        _generate_proposal()
                    except Exception:
                        pass

                threading.Thread(target=_generate_proposal_background, daemon=True).start()
        self._log_ui_memory_retrieval(
            project_id=project.id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=body,
            responder_result=responder_result,
        )
        if frustration_detected:
            triage = triage_frustration(self.store, project_id=project.id, objective_id=objective_id)
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="operator_frustration",
                    project_id=project.id,
                    objective_id=objective_id,
                    visibility="model_visible",
                    author_type="operator",
                    author_id=(author or "").strip(),
                    content=body,
                    metadata={
                        "triage": {
                            "objective_id": triage.objective_id,
                            "likely_causes": triage.likely_causes,
                            "recommendation": triage.recommendation,
                            "confidence": triage.confidence,
                        },
                        "derived_from": "operator_comment",
                    },
                )
            )
            if objective_id:
                self.store.update_objective_status(objective_id, ObjectiveStatus.INVESTIGATING)
        reply_record = ContextRecord(
            id=new_id("context"),
            record_type="harness_reply",
            project_id=project.id,
            objective_id=objective_id,
            task_id=task_id,
            visibility="operator_visible",
            author_type="system",
            content=responder_result.reply,
            metadata={
                "reply_to": record.id,
                "recommended_action": responder_result.recommended_action,
                "evidence_refs": responder_result.evidence_refs,
                "mode_shift": responder_result.mode_shift,
                "retrieved_memories": [serialize_dataclass(memory) for memory in responder_result.retrieved_memories],
                "llm_backend": responder_result.llm_backend,
                "prompt_path": responder_result.prompt_path,
                "response_path": responder_result.response_path,
            },
        )
        self.store.create_context_record(reply_record)
        return {
            "comment": {
                "id": record.id,
                "author": record.author_id,
                "text": record.content,
                "objective_id": record.objective_id,
                "task_id": record.task_id,
                "created_at": record.created_at.isoformat(),
            },
            "reply": {
                "id": reply_record.id,
                "text": reply_record.content,
                "objective_id": reply_record.objective_id,
                "task_id": reply_record.task_id,
                "created_at": reply_record.created_at.isoformat(),
                "recommended_action": responder_result.recommended_action,
                "evidence_refs": responder_result.evidence_refs,
                "mode_shift": responder_result.mode_shift,
                "retrieved_memories": [serialize_dataclass(memory) for memory in responder_result.retrieved_memories],
                "llm_backend": responder_result.llm_backend,
                "prompt_path": responder_result.prompt_path,
                "response_path": responder_result.response_path,
            },
            "frustration_detected": frustration_detected,
            "mermaid_proposal": proposal,
        }


    def add_operator_frustration(
        self,
        project_ref: str,
        text: str,
        author: str | None,
        objective_id: str | None = None,
    ) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        body = text.strip()
        if not body:
            raise ValueError("Frustration text must not be empty")
        if objective_id:
            objective = self.store.get_objective(objective_id)
            if objective is None or objective.project_id != project.id:
                raise ValueError(f"Unknown objective: {objective_id}")
        triage = triage_frustration(self.store, project_id=project.id, objective_id=objective_id)
        record = ContextRecord(
            id=new_id("context"),
            record_type="operator_frustration",
            project_id=project.id,
            objective_id=objective_id,
            visibility="model_visible",
            author_type="operator",
            author_id=(author or "").strip(),
            content=body,
            metadata={
                "triage": {
                    "objective_id": triage.objective_id,
                    "likely_causes": triage.likely_causes,
                    "recommendation": triage.recommendation,
                    "confidence": triage.confidence,
                }
            },
        )
        self.store.create_context_record(record)
        if objective_id:
            self.store.update_objective_status(objective_id, ObjectiveStatus.INVESTIGATING)
        return {
            "frustration": {
                "id": record.id,
                "author": record.author_id,
                "text": record.content,
                "objective_id": record.objective_id,
                "created_at": record.created_at.isoformat(),
                "triage": record.metadata["triage"],
            }
        }


    def _operator_comments(self, project_id: str) -> list[dict[str, object]]:
        comments = []
        for record in self.store.list_context_records(project_id=project_id, record_type="operator_comment"):
            comments.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "author": record.author_id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                }
            )
        return comments


    def _operator_frustrations(self, project_id: str) -> list[dict[str, object]]:
        frustrations = []
        for record in self.store.list_context_records(project_id=project_id, record_type="operator_frustration"):
            triage = record.metadata.get("triage", {})
            frustrations.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "author": record.author_id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "triage": triage,
                }
            )
        return frustrations


    def _action_receipts(self, project_id: str) -> list[dict[str, object]]:
        receipts = []
        for record in self.store.list_context_records(project_id=project_id, record_type="action_receipt"):
            text = record.content
            if text.startswith("Action receipt: "):
                text = text[len("Action receipt: "):]
            receipts.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "text": text,
                    "created_at": record.created_at.isoformat(),
                    "metadata": record.metadata,
                }
            )
        return receipts


    def _harness_replies(self, project_id: str) -> list[dict[str, object]]:
        replies = []
        for record in self.store.list_context_records(project_id=project_id, record_type="harness_reply"):
            replies.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "recommended_action": record.metadata.get("recommended_action", "none"),
                    "evidence_refs": record.metadata.get("evidence_refs", []),
                    "mode_shift": record.metadata.get("mode_shift", "none"),
                    "retrieved_memories": record.metadata.get("retrieved_memories", []),
                    "llm_backend": record.metadata.get("llm_backend", ""),
                    "prompt_path": record.metadata.get("prompt_path", ""),
                    "response_path": record.metadata.get("response_path", ""),
                }
            )
        return replies


    def _answer_operator_comment(
        self,
        *,
        project_id: str,
        objective_id: str | None,
        task_id: str | None,
        comment_text: str,
        frustration_detected: bool,
    ) -> ResponderResult:
        packet = self._build_responder_context_packet(
            project_id=project_id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=comment_text,
            frustration_detected=frustration_detected,
        )
        llm_result = self._answer_operator_comment_with_llm(
            packet=packet,
            project_id=project_id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=comment_text,
        )
        if llm_result is not None:
            return llm_result
        return ResponderResult(
            reply="Acknowledged. No LLM backend is available for a detailed response.",
            recommended_action="",
            evidence_refs=[],
            mode_shift="",
            retrieved_memories=[],
            llm_backend="",
            prompt_path="",
            response_path="",
        )


    @staticmethod
    def _comment_looks_like_frustration(text: str) -> bool:
        lowered = text.lower()
        triggers = [
            "frustrat",
            "annoy",
            "confus",
            "stuck",
            "what am i supposed",
            "doesn't make sense",
            "terrible",
            "bad ux",
        ]
        return any(trigger in lowered for trigger in triggers)


    def _comment_requests_mermaid_update(
        self,
        text: str,
        *,
        project_id: str,
        objective_id: str | None,
    ) -> bool:
        lowered = text.lower().strip()
        mermaid_terms = ("mermaid", "diagram", "control flow", "flowchart", "flow chart")
        update_terms = ("update", "revise", "regenerate", "rewrite", "reflect this", "change", "remove", "add", "fix")
        if any(term in lowered for term in mermaid_terms) and any(term in lowered for term in update_terms):
            return True
        latest_mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control") if objective_id else None
        proposal_pending = self._latest_mermaid_proposal(objective_id) is not None if objective_id else False
        in_mermaid_review = latest_mermaid is not None and latest_mermaid.status in {MermaidStatus.PAUSED, MermaidStatus.DRAFT}
        structural_terms = ("step", "loop", "gate", "branch", "path", "node", "box", "label", "exit condition", "planning elements")
        if in_mermaid_review and any(term in lowered for term in update_terms) and (
            proposal_pending or any(term in lowered for term in structural_terms)
        ):
            return True
        if lowered in {"do it", "do it.", "do that", "apply it", "make the changes", "make your changes", "go ahead", "use that"}:
            recent_turns = self._recent_conversation_turns(project_id=project_id, objective_id=objective_id)
            recent_text = "\n".join(turn.text.lower() for turn in recent_turns[-6:])
            return (
                "update the mermaid" in recent_text
                or "proposed mermaid update" in recent_text
                or "diagram should be revised" in recent_text
                or "revise that diagram" in recent_text
                or "make your changes to the diagram" in recent_text
            )
        return False


    def _enqueue_task_question(
        self,
        *,
        project_id: str,
        objective_id: str | None,
        task_id: str,
        comment_record: ContextRecord,
        frustration_detected: bool,
    ) -> dict[str, object]:
        queued_at = _dt.datetime.now(_dt.timezone.utc)
        job_id = new_id("replyjob")
        pending_record = ContextRecord(
            id=new_id("context"),
            record_type="harness_reply_pending",
            project_id=project_id,
            objective_id=objective_id,
            task_id=task_id,
            visibility="operator_visible",
            author_type="system",
            content="Waiting on harness response…",
            metadata={
                "reply_to": comment_record.id,
                "status": "pending",
                "job_id": job_id,
                "queued_at": queued_at.isoformat(),
            },
        )
        self.store.create_context_record(pending_record)

        from ..ui import _run_task_question_job
        mp.Process(
            target=_run_task_question_job,
            kwargs={
                "db_path": str(self.ctx.config.db_path),
                "workspace_root": str(self.ctx.config.workspace_root),
                "log_path": (str(self.ctx.config.log_path) if self.ctx.config.log_path is not None else None),
                "config_file": None,
                "project_id": project_id,
                "objective_id": objective_id,
                "task_id": task_id,
                "comment_record_id": comment_record.id,
                "comment_text": comment_record.content,
                "frustration_detected": frustration_detected,
                "job_id": job_id,
                "queued_at_iso": queued_at.isoformat(),
            },
            daemon=True,
        ).start()
        return {
            "comment": {
                "id": comment_record.id,
                "author": comment_record.author_id,
                "text": comment_record.content,
                "objective_id": comment_record.objective_id,
                "task_id": comment_record.task_id,
                "created_at": comment_record.created_at.isoformat(),
            },
            "reply": {
                "id": pending_record.id,
                "text": pending_record.content,
                "objective_id": pending_record.objective_id,
                "task_id": pending_record.task_id,
                "created_at": pending_record.created_at.isoformat(),
                "status": "pending",
                "job_id": job_id,
                "queued_at": queued_at.isoformat(),
            },
            "frustration_detected": frustration_detected,
        }

