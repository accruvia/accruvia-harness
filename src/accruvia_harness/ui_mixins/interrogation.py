"""HarnessUIDataService interrogation review methods."""
from __future__ import annotations

import json
import re
from typing import Any

from ..domain import ContextRecord, InterrogationReview, Objective, new_id
from ._shared import _INTERROGATION_RED_TEAM_MAX_ROUNDS

class InterrogationMixin:

    def complete_interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        review = self._interrogation_review(objective_id)
        if review.get("completed"):
            return {"interrogation_review": review}
        if review.get("generated_by") == "deterministic":
            review = self._generate_interrogation_review(objective_id)
        self._persist_interrogation_record("interrogation_completed", objective, review)
        self.reconcile_objective_workflow(objective.id)
        return {"interrogation_review": self._interrogation_review(objective.id)}


    def _interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        deterministic = self._deterministic_interrogation_review(objective_id)
        completions = self.store.list_context_records(objective_id=objective_id, record_type="interrogation_completed")
        latest_completion = completions[-1] if completions else None
        if latest_completion is not None:
            return self._recorded_interrogation_review(latest_completion, completed=True)

        drafts = self.store.list_context_records(objective_id=objective_id, record_type="interrogation_draft")
        latest_draft = drafts[-1] if drafts else None
        if latest_draft is not None:
            return self._recorded_interrogation_review(latest_draft, completed=False)

        if deterministic["plan_elements"]:
            generated = self._generate_interrogation_review(objective_id)
            if generated.get("generated_by") != "deterministic":
                self._persist_interrogation_record("interrogation_draft", objective, generated)
                drafts = self.store.list_context_records(objective_id=objective_id, record_type="interrogation_draft")
                latest_draft = drafts[-1] if drafts else None
                if latest_draft is not None:
                    return self._recorded_interrogation_review(latest_draft, completed=False)
        return deterministic


    def _generate_interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        deterministic = self._deterministic_interrogation_review(objective_id)
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is None:
            return deterministic

        intent_model = self.store.latest_intent_model(objective_id)
        comments = self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")[-6:]
        orchestrator = self._red_team_loop_orchestrator(llm_router)
        initial_inputs = {
            "objective_title": objective.title,
            "objective_summary": objective.summary,
            "intent_summary": intent_model.intent_summary if intent_model else "",
            "success_definition": intent_model.success_definition if intent_model else "",
            "non_negotiables": list(intent_model.non_negotiables) if intent_model else [],
            "recent_comments": [r.content for r in comments],
            "deterministic_review": deterministic,
        }

        def stopping_predicate(output, reviewer_results, round_number):
            if bool(output.get("ready_for_mermaid_review")):
                return True
            findings = list(output.get("red_team_findings") or [])
            return not findings

        loop_result = orchestrator.execute(
            generator_skill_name="interrogation",
            reviewer_skill_names=None,
            initial_inputs=initial_inputs,
            stopping_predicate=stopping_predicate,
            max_rounds=_INTERROGATION_RED_TEAM_MAX_ROUNDS,
            project_id=objective.project_id,
            loop_label="interrogation",
            loop_key=objective_id,
        )
        if not loop_result.success or not loop_result.final_output:
            return deterministic
        parsed = loop_result.final_output
        last_round = loop_result.history[-1] if loop_result.history else None
        return InterrogationReview(
            completed=False,
            summary=str(parsed.get("summary") or ""),
            plan_elements=list(parsed.get("plan_elements") or []),
            questions=list(parsed.get("questions") or []),
            generated_by="llm",
            backend=last_round.generator_result.llm_backend if last_round else "",
            prompt_path=last_round.generator_result.prompt_path if last_round else "",
            response_path=last_round.generator_result.response_path if last_round else "",
            red_team_rounds=loop_result.rounds_completed,
            red_team_stop_reason=loop_result.stop_reason,
        ).to_dict()


    def _deterministic_interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        intent_model = self.store.latest_intent_model(objective_id)
        desired_outcome = (intent_model.intent_summary if intent_model is not None else "").strip()
        success_definition = (intent_model.success_definition if intent_model is not None else "").strip()
        non_negotiables = list(intent_model.non_negotiables) if intent_model is not None else []

        plan_elements: list[str] = []
        if desired_outcome:
            plan_elements.append(f"Desired outcome: {desired_outcome}")
        if success_definition:
            plan_elements.append(f"Success definition: {success_definition}")
        if non_negotiables:
            plan_elements.append("Non-negotiables: " + ", ".join(non_negotiables[:4]))

        questions: list[str] = []
        if desired_outcome:
            questions.append("What concrete operator experience should feel different if this objective succeeds?")
        else:
            questions.append("What exact outcome should exist before the harness starts planning?")
        if success_definition:
            questions.append("What evidence would prove this objective is complete instead of only improved?")
        else:
            questions.append("How should the harness measure success for this objective?")
        questions.append("What is the most likely way the current plan could still miss your intent?")
        questions.append("What ambiguity should be resolved before Mermaid review?")
        return InterrogationReview(
            completed=False,
            summary="The harness should interrogate the objective and self-red-team the plan before Mermaid review.",
            plan_elements=plan_elements,
            questions=questions,
            generated_by="deterministic",
        ).to_dict()


    def _recorded_interrogation_review(self, record: ContextRecord, *, completed: bool) -> dict[str, object]:
        return InterrogationReview(
            completed=completed,
            summary=record.content,
            plan_elements=list(record.metadata.get("plan_elements") or []),
            questions=list(record.metadata.get("questions") or []),
            generated_by=record.metadata.get("generated_by", "deterministic"),
            backend=record.metadata.get("backend"),
        ).to_dict()


    def _persist_interrogation_record(self, record_type: str, objective: Objective, review: dict[str, object]) -> None:
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type=record_type,
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="system",
                content=str(review["summary"]),
                metadata={
                    "plan_elements": review["plan_elements"],
                    "questions": review["questions"],
                    "generated_by": review.get("generated_by", "deterministic"),
                    "backend": review.get("backend"),
                    "prompt_path": review.get("prompt_path"),
                    "response_path": review.get("response_path"),
                },
            )
        )


    def _should_auto_complete_interrogation(self, objective_id: str) -> bool:
        review = self._interrogation_review(objective_id)
        if review.get("completed"):
            return False
        questions = list(review.get("questions") or [])
        if not questions:
            return False
        intent_model = self.store.latest_intent_model(objective_id)
        created_at = intent_model.created_at.isoformat() if intent_model is not None else ""
        answers = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")
            if not created_at or record.created_at.isoformat() >= created_at
        ]
        if len(answers) >= len(questions):
            return True
        return any(len((record.content or "").strip()) >= 48 for record in answers)


    def _build_interrogation_prompt(self, objective_id: str, deterministic: dict[str, object]) -> str:
        objective = self.store.get_objective(objective_id)
        intent_model = self.store.latest_intent_model(objective_id)
        comments = self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")[-6:]
        return (
            "You are red-teaming a software objective before process review.\n"
            "Your job is to interrogate the objective, extract the likely plan elements, and list the sharpest unresolved questions.\n"
            "Return JSON only with keys: summary, plan_elements, questions.\n"
            "summary: short paragraph\n"
            "plan_elements: array of concise strings\n"
            "questions: array of concise red-team questions\n\n"
            f"Objective title: {objective.title if objective else ''}\n"
            f"Objective summary: {objective.summary if objective else ''}\n"
            f"Intent summary: {intent_model.intent_summary if intent_model else ''}\n"
            f"Success definition: {intent_model.success_definition if intent_model else ''}\n"
            f"Non-negotiables: {json.dumps(intent_model.non_negotiables if intent_model else [])}\n"
            f"Recent operator comments: {json.dumps([record.content for record in comments], indent=2)}\n"
            f"Current deterministic review: {json.dumps(deterministic, indent=2, sort_keys=True)}\n"
        )


    def _parse_interrogation_response(self, text: str) -> dict[str, object] | None:
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
            plan_elements = [str(item).strip() for item in list(payload.get("plan_elements") or []) if str(item).strip()]
            questions = [str(item).strip() for item in list(payload.get("questions") or []) if str(item).strip()]
            if summary and plan_elements and questions:
                return {
                    "summary": summary,
                    "plan_elements": plan_elements,
                    "questions": questions,
                }
        return None

