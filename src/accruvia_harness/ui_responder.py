from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class RetrievedMemory:
    summary: str
    source: str = ""


@dataclass(slots=True)
class ConversationTurn:
    role: str
    text: str
    created_at: str


@dataclass(slots=True)
class ObjectiveResponderContext:
    objective_id: str
    title: str
    status: str
    summary: str
    intent_summary: str
    success_definition: str
    non_negotiables: list[str] = field(default_factory=list)
    mermaid_status: str = ""
    mermaid_summary: str = ""


@dataclass(slots=True)
class TaskResponderContext:
    task_id: str
    title: str
    status: str
    strategy: str
    objective: str
    analysis_summary: str = ""
    failure_message: str = ""
    root_cause_hint: str = ""
    backend_failure_kind: str = ""
    backend_failure_explanation: str = ""
    evidence_to_inspect: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunResponderContext:
    run_id: str
    attempt: int
    status: str
    summary: str
    available_sections: list[str] = field(default_factory=list)
    section_previews: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ResponderContextPacket:
    project_id: str
    project_name: str
    mode: str
    next_action_title: str
    next_action_body: str
    objective: ObjectiveResponderContext | None = None
    task: TaskResponderContext | None = None
    run: RunResponderContext | None = None
    recent_turns: list[ConversationTurn] = field(default_factory=list)
    frustration_detected: bool = False
    retrieved_memories: list[RetrievedMemory] = field(default_factory=list)
    interrogation_question: str = ""
    interrogation_remaining: int = 0


@dataclass(slots=True)
class ResponderResult:
    reply: str
    recommended_action: str = "none"
    evidence_refs: list[str] = field(default_factory=list)
    mode_shift: str = "none"
    retrieved_memories: list[RetrievedMemory] = field(default_factory=list)
    llm_backend: str = ""
    prompt_path: str = ""
    response_path: str = ""


def answer_ui_message(packet: ResponderContextPacket, message: str) -> ResponderResult:
    lowered = message.strip().lower()
    last_harness_turn = ""
    for turn in reversed(packet.recent_turns):
        if turn.role == "harness":
            last_harness_turn = turn.text.lower()
            break

    if packet.mode == "interrogation_review":
        if packet.interrogation_question:
            remaining_text = (
                f" After this, I still have {packet.interrogation_remaining} more red-team question"
                f"{'' if packet.interrogation_remaining == 1 else 's'}."
                if packet.interrogation_remaining > 0
                else ""
            )
            return ResponderResult(
                reply=(
                    "Recorded. Before we move into Mermaid review, I need one more clarification. "
                    + packet.interrogation_question
                    + remaining_text
                ),
                recommended_action="answer_prompt",
                retrieved_memories=packet.retrieved_memories,
            )
        return ResponderResult(
            reply=(
                "Interrogation is complete. The next step is Mermaid review so we can confirm the control logic "
                "matches your intended flow before execution."
            ),
            recommended_action="review_mermaid",
            retrieved_memories=packet.retrieved_memories,
        )

    if packet.task is not None and (
        "what failed" in lowered
        or "why did this task fail" in lowered
        or "why did it fail" in lowered
        or "why did this fail" in lowered
        or "explain the failure" in lowered
        or "explain why" in lowered
    ):
        detail_bits = []
        if packet.task.analysis_summary:
            detail_bits.append(packet.task.analysis_summary)
        if packet.task.backend_failure_explanation:
            detail_bits.append(packet.task.backend_failure_explanation)
        if packet.task.failure_message:
            detail_bits.append(f"Raw failure: {packet.task.failure_message}")
        if packet.task.root_cause_hint and packet.task.root_cause_hint != packet.task.failure_message:
            detail_bits.append(f"Root-cause hint: {packet.task.root_cause_hint}")
        if detail_bits:
            return ResponderResult(
                reply=" ".join(detail_bits),
                recommended_action="review_run" if packet.run is not None else "none",
                evidence_refs=_run_refs(packet),
                retrieved_memories=packet.retrieved_memories,
            )

    if packet.task is not None and (
        "should i retry" in lowered
        or "retry or waive" in lowered
        or "waive" in lowered
    ):
        failure_cause = packet.task.failure_message or packet.task.analysis_summary or "the latest failed run"
        infra_like = "all worker backends failed" in failure_cause.lower() or "executor/infrastructure" in failure_cause.lower()
        if packet.task.backend_failure_kind in {"quota", "auth", "backend_unavailable"}:
            infra_like = True
        return ResponderResult(
            reply=(
                (f"This looks infrastructure-related rather than like a completed product judgment. " if infra_like else "")
                + f"Retry if {failure_cause} looks transient or environmental. "
                + "Waive only if the task is obsolete, superseded, or no longer needed for promotion. "
                "If the failure points to a real implementation gap, retry or replace the work instead of waiving it."
            ),
            recommended_action="review_run" if packet.run is not None else "none",
            evidence_refs=_run_refs(packet),
            retrieved_memories=packet.retrieved_memories,
        )

    if packet.task is not None and (
        "what evidence should i inspect next" in lowered
        or "what should i inspect next" in lowered
        or "what evidence next" in lowered
    ):
        available = ", ".join(packet.task.evidence_to_inspect[:4]) if packet.task.evidence_to_inspect else (
            ", ".join(packet.run.available_sections[:4]) if packet.run is not None else ""
        )
        failure_bits = []
        if packet.task.analysis_summary:
            failure_bits.append(packet.task.analysis_summary)
        if packet.task.backend_failure_explanation:
            failure_bits.append(packet.task.backend_failure_explanation)
        if packet.task.failure_message:
            failure_bits.append(f"Raw failure: {packet.task.failure_message}")
        evidence_text = (
            f"Inspect these artifacts next: {available}."
            if available
            else "Inspect the latest run artifact bundle and any persisted report or stderr output for this task."
        )
        backend_check_text = "For this task, I would first confirm whether the backend failure was quota, auth, or executor configuration before deciding to retry or waive it."
        if packet.task.backend_failure_kind == "quota":
            backend_check_text = "This looks like a quota or credit exhaustion issue. Confirm the CLI stderr or provider account state before retrying."
        elif packet.task.backend_failure_kind == "auth":
            backend_check_text = "This looks like an authentication issue. Confirm the CLI stderr, token state, or login/session before retrying."
        elif packet.task.backend_failure_kind == "backend_unavailable":
            backend_check_text = "This looks like backend or executor availability trouble. Confirm the worker stderr and executor configuration before retrying."
        return ResponderResult(
            reply=" ".join(
                part for part in [
                    "Start with the latest run for this exact task.",
                    " ".join(failure_bits) if failure_bits else "",
                    evidence_text,
                    backend_check_text,
                ] if part
            ),
            recommended_action="review_run" if packet.run is not None else "none",
            evidence_refs=_run_refs(packet),
            retrieved_memories=packet.retrieved_memories,
        )

    if "investigation" in lowered:
        return ResponderResult(
            reply=(
                "I recommend investigation mode. We should compare the current Mermaid, your intent, and the latest run "
                "evidence before making more code changes."
            ),
            recommended_action="open_investigation",
            evidence_refs=_run_refs(packet),
            mode_shift="investigation",
            retrieved_memories=packet.retrieved_memories,
        )

    if lowered in {"how", "how?", "how do i do that?", "how do i do that", "how do i review it?", "how do i review it"}:
        if "review the latest run" in last_harness_turn or "latest run" in last_harness_turn:
            if packet.run is None:
                return ResponderResult(
                    reply="There is no latest run yet. Start the current implementation step first.",
                    recommended_action="start_run",
                )
            section_text = ", ".join(packet.run.available_sections[:4]) or "no readable artifacts yet"
            return ResponderResult(
                reply=(
                    f"To review the latest run, use `Review latest run output`. "
                    f"The current run is attempt {packet.run.attempt} with status {packet.run.status}. "
                    f"Right now the most relevant evidence is {section_text}. "
                    "If you want, ask me to summarize the latest run in plain English."
                ),
                recommended_action="review_run",
                evidence_refs=_run_refs(packet),
                retrieved_memories=packet.retrieved_memories,
            )

    if packet.run is not None and (
        "review" in lowered
        or "where do i look" in lowered
        or "don't get it" in lowered
        or "do not get it" in lowered
        or "where do i click" in lowered
    ):
        section_text = ", ".join(packet.run.available_sections[:4]) or "no readable artifacts yet"
        return ResponderResult(
            reply=(
                f"To review the latest run, click `Review latest run output` just below this input box. "
                f"That opens the evidence pane for attempt {packet.run.attempt} with status {packet.run.status}. "
                f"Start with {section_text}. If you do not want to read the raw evidence yourself, ask me to summarize the latest run."
            ),
            recommended_action="review_run",
            evidence_refs=_run_refs(packet),
            retrieved_memories=packet.retrieved_memories,
        )

    if "what do you need from me" in lowered or "need from me" in lowered:
        if "summarize the latest run" in last_harness_turn or "summarize the run" in last_harness_turn:
            if packet.run is None:
                return ResponderResult(
                    reply="I do not need anything from you yet because there is no run to summarize.",
                    recommended_action="start_run",
                )
            return ResponderResult(
                reply=(
                    "Nothing. I already have enough evidence from the latest run to summarize it. "
                    + _run_plain_summary(packet)
                ),
                recommended_action="review_run",
                evidence_refs=_run_refs(packet),
                retrieved_memories=packet.retrieved_memories,
            )

    if "inspect" in lowered or "output" in lowered or "what happened" in lowered or "summarize the latest run" in lowered or "summarize the run" in lowered:
        if packet.run is None:
            return ResponderResult(
                reply=(
                    "There is no run output to inspect yet. The current implementation step has not started. "
                    "Start the step first, or ask to investigate if the plan itself feels wrong."
                ),
                recommended_action="start_run",
            )
        return ResponderResult(
            reply=_run_plain_summary(packet),
            recommended_action="review_run",
            evidence_refs=_run_refs(packet),
            retrieved_memories=packet.retrieved_memories,
        )

    if "what am i supposed" in lowered or "what do i do next" in lowered or "what's next" in lowered or "next step" in lowered or lowered == "next":
        reply = packet.next_action_body
        if packet.run is not None:
            summary = f" {packet.run.summary}" if packet.run.summary else ""
            reply = (
                f"The next step is to review the latest run for {packet.objective.title if packet.objective else 'this objective'}. "
                f"The run is attempt {packet.run.attempt} with status {packet.run.status}.{summary}"
            )
            return ResponderResult(
                reply=reply.strip(),
                recommended_action="review_run",
                evidence_refs=_run_refs(packet),
                retrieved_memories=packet.retrieved_memories,
            )
        if packet.task is not None:
            return ResponderResult(
                reply=packet.next_action_body,
                recommended_action="start_run",
                retrieved_memories=packet.retrieved_memories,
            )
        return ResponderResult(
            reply=f"The next step is: {packet.next_action_body}",
            recommended_action="answer_prompt",
            retrieved_memories=packet.retrieved_memories,
        )

    if packet.frustration_detected:
        memory_hint = ""
        if packet.retrieved_memories:
            memory_hint = f" Related memory: {packet.retrieved_memories[0].summary}"
        return ResponderResult(
            reply=(
                "I think you're frustrated for a real reason. "
                "The most likely issue is that intent and observed behavior have drifted apart. "
                "I recommend investigation mode before more execution." + memory_hint
            ),
            recommended_action="open_investigation",
            evidence_refs=_run_refs(packet),
            mode_shift="investigation",
            retrieved_memories=packet.retrieved_memories,
        )

    current_target = packet.objective.title if packet.objective else packet.project_name
    memory_hint = _memory_hint(packet.retrieved_memories)
    if packet.run is not None:
        return ResponderResult(
            reply=(
                f"I recorded your guidance for {current_target}. "
                f"The current run is attempt {packet.run.attempt} with status {packet.run.status}. "
                "If you want to inspect it yourself, click `Review latest run output` below this input. "
                "If you want me to interpret it, ask me to summarize the latest run."
                + memory_hint
            ),
            recommended_action="review_run",
            evidence_refs=_run_refs(packet),
            retrieved_memories=packet.retrieved_memories,
        )
    if packet.task is not None:
        return ResponderResult(
            reply=(
                f"I recorded your guidance for {current_target}. "
                "The next action is the current implementation step. I can explain it, start it, or help investigate if it feels wrong."
                + memory_hint
            ),
            recommended_action="start_run",
            retrieved_memories=packet.retrieved_memories,
        )
    return ResponderResult(
        reply=(
            f"I recorded your guidance for {current_target}. "
            "I can help clarify the objective, refine the process, or explain the next required step."
            + memory_hint
        ),
        recommended_action="answer_prompt",
        retrieved_memories=packet.retrieved_memories,
    )


def _run_refs(packet: ResponderContextPacket) -> list[str]:
    refs: list[str] = []
    if packet.run is not None:
        refs.append(f"run:{packet.run.run_id}")
        refs.extend(f"artifact:{label}" for label in packet.run.available_sections[:4])
    return refs


def _run_plain_summary(packet: ResponderContextPacket) -> str:
    assert packet.run is not None
    target = packet.objective.title if packet.objective else "this objective"
    pieces = [
        f"For {target}, the latest run is attempt {packet.run.attempt} with status {packet.run.status}."
    ]
    if packet.run.summary:
        pieces.append(packet.run.summary)

    previews = packet.run.section_previews
    if "report" in previews:
        pieces.append(f"Report: {previews['report']}")
    if "test output" in previews:
        pieces.append(f"Tests: {previews['test output']}")
    if "compile output" in previews:
        pieces.append(f"Compile: {previews['compile output']}")
    if "worker stderr" in previews:
        pieces.append(f"Worker stderr: {previews['worker stderr']}")
    elif "codex worker stderr" in previews:
        pieces.append(f"Worker stderr: {previews['codex worker stderr']}")
    elif "llm stderr" in previews:
        pieces.append(f"LLM stderr: {previews['llm stderr']}")

    if len(pieces) == 2 and packet.run.available_sections:
        pieces.append(
            "Readable evidence is available in: " + ", ".join(packet.run.available_sections[:4]) + "."
        )
    return " ".join(piece.strip() for piece in pieces if piece.strip())


def _memory_hint(memories: list[RetrievedMemory]) -> str:
    if not memories:
        return ""
    return f" Relevant prior context: {memories[0].summary}"
