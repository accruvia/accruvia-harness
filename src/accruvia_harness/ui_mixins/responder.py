"""HarnessUIDataService responder methods."""
from __future__ import annotations

import json
import re
from typing import Any

from ..domain import (
    ContextRecord, Run, RunStatus, Task, TaskStatus, new_id, serialize_dataclass,
)

from ._shared import _AttrDict, ConversationTurn, ResponderResult, ResponderContextPacket

class ResponderMixin:

    def _answer_operator_comment_with_llm(
        self,
        *,
        packet: ResponderContextPacket,
        project_id: str,
        objective_id: str | None,
        task_id: str | None,
        comment_text: str,
    ) -> ResponderResult | None:
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is None or not getattr(llm_router, "executors", {}):
            return None
        prompt = self._build_ui_responder_prompt(
            packet=packet,
            project_id=project_id,
            objective_id=objective_id,
            task_id=task_id,
            comment_text=comment_text,
        )
        run_dir = self.workspace_root / "ui_responder" / (objective_id or project_id) / new_id("reply")
        run_dir.mkdir(parents=True, exist_ok=True)
        task = Task(
            id=new_id("ui_reply_task"),
            project_id=project_id,
            title=f"UI response for {packet.objective.title if packet.objective else packet.project_name}",
            objective="Answer the operator directly from current harness state and full available context.",
            strategy="ui_responder",
            status=TaskStatus.COMPLETED,
        )
        run = Run(
            id=new_id("ui_reply_run"),
            task_id=task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary=f"UI reply for {objective_id or project_id}",
        )
        from ..skills import SkillInvocation, invoke_skill
        skill = self._skill_registry().get("ui_responder")
        invocation = SkillInvocation(
            skill_name="ui_responder",
            inputs={
                "operator_message": comment_text,
                "context_payload": {"prompt_envelope": prompt},
            },
            task=task,
            run=run,
            run_dir=run_dir,
        )
        skill_result = invoke_skill(skill, invocation, llm_router, telemetry=getattr(self.ctx, "telemetry", None))
        if not skill_result.success:
            return None
        parsed = skill_result.output
        return ResponderResult(
            reply=str(parsed.get("reply") or ""),
            recommended_action=str(parsed.get("recommended_action") or "none"),
            evidence_refs=list(parsed.get("evidence_refs") or []),
            mode_shift=str(parsed.get("mode_shift") or "none"),
            retrieved_memories=packet.retrieved_memories,
            llm_backend=skill_result.llm_backend or "",
            prompt_path=skill_result.prompt_path or "",
            response_path=skill_result.response_path or "",
        )


    def _build_ui_responder_prompt(
        self,
        *,
        packet: ResponderContextPacket,
        project_id: str,
        objective_id: str | None,
        task_id: str | None,
        comment_text: str,
    ) -> str:
        project = self.store.get_project(project_id)
        objective = self.store.get_objective(objective_id) if objective_id else None
        intent_model = self.store.latest_intent_model(objective_id) if objective_id else None
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control") if objective_id else None
        interrogation_review = self._interrogation_review(objective_id) if objective_id else {}
        task = self.store.get_task(task_id) if task_id else None
        run = None
        if task is not None:
            task_runs = self.store.list_runs(task.id)
            run = task_runs[-1] if task_runs else None
        else:
            task, run = self._latest_linked_task_and_run(project_id=project_id, objective_id=objective_id)
        run_output = self.run_cli_output(run.id) if run is not None else {}
        task_insight = self.task_failure_insight(task.id) if task is not None else {}
        all_records = self.store.list_context_records(objective_id=objective_id) if objective_id else self.store.list_context_records(project_id=project_id)
        context_records = [
            {
                "record_type": record.record_type,
                "created_at": record.created_at.isoformat(),
                "author_type": record.author_type,
                "author_id": record.author_id,
                "visibility": record.visibility,
                "task_id": record.task_id,
                "run_id": record.run_id,
                "content": record.content,
                "metadata": record.metadata,
            }
            for record in all_records
        ]
        payload = {
            "project": serialize_dataclass(project) if project is not None else None,
            "mode": packet.mode,
            "next_action": {
                "title": packet.next_action_title,
                "body": packet.next_action_body,
            },
            "objective": serialize_dataclass(objective) if objective is not None else None,
            "intent_model": serialize_dataclass(intent_model) if intent_model is not None else None,
            "interrogation_review": interrogation_review,
            "mermaid": (
                {
                    "status": mermaid.status.value,
                    "summary": mermaid.summary,
                    "content": mermaid.content,
                    "version": mermaid.version,
                    "blocking_reason": mermaid.blocking_reason,
                }
                if mermaid is not None
                else None
            ),
            "latest_task": serialize_dataclass(task) if task is not None else None,
            "selected_task_insight": task_insight if task is not None else None,
            "latest_run": serialize_dataclass(run) if run is not None else None,
            "latest_run_output": run_output,
            "recent_turns": [serialize_dataclass(turn) for turn in packet.recent_turns],
            "retrieved_memories": [serialize_dataclass(memory) for memory in packet.retrieved_memories],
            "frustration_detected": packet.frustration_detected,
            "all_context_records": context_records,
            "operator_message": comment_text,
        }
        return (
            "You are the accrivia-harness UI responder.\n"
            "Answer the operator's latest message directly and concretely.\n"
            "Use the full current objective context, not just the latest run.\n"
            "Do not dodge the question. Do not default to boilerplate about reviewing output unless that directly answers the question.\n"
            "If the operator asks where red-team belongs, answer that directly from the planning/control-flow context.\n"
            "Prefer plain language and explain what stage the operator is in when relevant.\n"
            "Return JSON only with keys: reply, recommended_action, evidence_refs, mode_shift.\n"
            "reply: short plain-language answer to the operator\n"
            "recommended_action: one of none, answer_prompt, review_mermaid, review_run, start_run, open_investigation\n"
            "evidence_refs: array of short strings\n"
            "mode_shift: one of none, investigation\n\n"
            f"Context:\n{json.dumps(payload, indent=2, sort_keys=True)}\n"
        )


    def _build_responder_context_packet(
        self,
        *,
        project_id: str,
        objective_id: str | None,
        task_id: str | None,
        comment_text: str,
        frustration_detected: bool,
    ) -> ResponderContextPacket:
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        objective = self.store.get_objective(objective_id) if objective_id else None
        intent_model = self.store.latest_intent_model(objective_id) if objective_id else None
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control") if objective_id else None
        next_action = self._next_action_for_context(objective_id)
        task = self.store.get_task(task_id) if task_id else None
        if task is not None and task.project_id != project_id:
            raise ValueError(f"Unknown task for project: {task_id}")
        run = None
        if task is not None:
            task_runs = self.store.list_runs(task.id)
            run = task_runs[-1] if task_runs else None
        else:
            task, run = self._latest_linked_task_and_run(project_id=project_id, objective_id=objective_id)
        run_context = None
        if run is not None:
            run_context = _AttrDict(
                run_id=run.id,
                attempt=run.attempt,
                status=run.status.value,
                summary=(run.summary or "").strip(),
                available_sections=[section.label for section in self._run_output_sections(run.id)],
                section_previews={
                    section.label: self._truncate_text(section.content, 220)
                    for section in self._run_output_sections(run.id)
                },
            )
        task_context = None
        if task is not None:
            insight = self.task_failure_insight(task.id)
            task_context = _AttrDict(
                task_id=task.id,
                title=task.title,
                status=task.status.value,
                strategy=task.strategy,
                objective=task.objective,
                analysis_summary=str(insight.get("analysis_summary") or ""),
                failure_message=str(insight.get("failure_message") or ""),
                root_cause_hint=str(insight.get("root_cause_hint") or ""),
                backend_failure_kind=str(insight.get("backend_failure_kind") or ""),
                backend_failure_explanation=str(insight.get("backend_failure_explanation") or ""),
                evidence_to_inspect=[str(item) for item in list(insight.get("suggested_evidence") or []) if str(item)],
            )
        objective_context = None
        if objective is not None:
            objective_context = _AttrDict(
                objective_id=objective.id,
                title=objective.title,
                status=objective.status.value,
                summary=objective.summary,
                intent_summary=(intent_model.intent_summary if intent_model is not None else ""),
                success_definition=(intent_model.success_definition if intent_model is not None else ""),
                non_negotiables=(intent_model.non_negotiables if intent_model is not None else []),
                mermaid_status=(mermaid.status.value if mermaid is not None else ""),
                mermaid_summary=(mermaid.summary if mermaid is not None else ""),
            )
        retrieved_memories = []
        if self.memory_provider is not None:
            retrieved_memories = self.memory_provider.retrieve(
                project_id=project.id,
                objective_id=objective_id,
                query_text=comment_text,
                limit=4,
            )
        current_mode = "empty"
        interrogation_question = ""
        interrogation_remaining = 0
        if objective is not None:
            current_mode = self._focus_mode_for_objective(objective.id)
            if current_mode == "interrogation_review":
                review = self._interrogation_review(objective.id)
                questions = list(review.get("questions") or [])
                intent_created_at = intent_model.created_at.isoformat() if intent_model is not None else ""
                relevant_answers = [
                    record
                    for record in self.store.list_context_records(objective_id=objective.id, record_type="operator_comment")
                    if not intent_created_at or record.created_at.isoformat() >= intent_created_at
                ]
                question_index = min(len(relevant_answers), max(0, len(questions) - 1))
                if questions:
                    interrogation_question = questions[question_index]
                    interrogation_remaining = max(0, len(questions) - question_index - 1)
        return ResponderContextPacket(
            project_id=project.id,
            project_name=project.name,
            mode=current_mode,
            next_action_title=next_action["title"],
            next_action_body=next_action["body"],
            objective=objective_context,
            task=task_context,
            run=run_context,
            recent_turns=self._recent_conversation_turns(project_id=project_id, objective_id=objective_id, task_id=task_id),
            frustration_detected=frustration_detected,
            retrieved_memories=retrieved_memories,
            interrogation_question=interrogation_question,
            interrogation_remaining=interrogation_remaining,
        )


    def _parse_ui_responder_response(self, text: str) -> dict[str, object] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        candidates.extend(fenced)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            reply = str(payload.get("reply") or "").strip()
            if not reply:
                continue
            recommended_action = str(payload.get("recommended_action") or "none").strip() or "none"
            mode_shift = str(payload.get("mode_shift") or "none").strip() or "none"
            evidence_refs = [
                str(item).strip()
                for item in list(payload.get("evidence_refs") or [])
                if str(item).strip()
            ]
            return {
                "reply": reply,
                "recommended_action": recommended_action,
                "mode_shift": mode_shift,
                "evidence_refs": evidence_refs,
            }
        return None


    def _log_ui_memory_retrieval(
        self,
        *,
        project_id: str,
        objective_id: str | None,
        task_id: str | None,
        comment_text: str,
        responder_result: ResponderResult,
    ) -> None:
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="ui_memory_retrieval",
                project_id=project_id,
                objective_id=objective_id,
                task_id=task_id,
                visibility="system_only",
                author_type="system",
                content=comment_text,
                metadata={
                    "retrieved_count": len(responder_result.retrieved_memories),
                    "retrieved_memories": [serialize_dataclass(memory) for memory in responder_result.retrieved_memories],
                    "recommended_action": responder_result.recommended_action,
                    "mode_shift": responder_result.mode_shift,
                    "evidence_refs": responder_result.evidence_refs,
                },
            )
        )


    def _recent_conversation_turns(self, *, project_id: str, objective_id: str | None, task_id: str | None = None) -> list[ConversationTurn]:
        turns: list[ConversationTurn] = []
        for record_type, role in (("operator_comment", "operator"), ("harness_reply", "harness")):
            for record in self.store.list_context_records(
                project_id=project_id,
                objective_id=objective_id,
                task_id=task_id,
                record_type=record_type,
            ):
                turns.append(
                    ConversationTurn(
                        role=role,
                        text=record.content,
                        created_at=record.created_at.isoformat(),
                    )
                )
        turns.sort(key=lambda item: item["created_at"])
        return turns[-10:]

