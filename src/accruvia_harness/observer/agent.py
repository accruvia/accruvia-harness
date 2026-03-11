"""LLM-powered interrogation agent that answers questions from harness evidence."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from .evidence_cache import EvidenceCache
from .query_client import HarnessQueryClient, QueryResult


SYSTEM_PROMPT = """\
You are an observer agent for the Accruvia Harness, a durable LLM workflow system \
for software development. You have read-only access to harness state: projects, tasks, \
runs, artifacts, evaluations, decisions, and events.

Your job is to answer the operator's questions about what the harness is doing, \
what happened, and what needs attention. Be concise and specific. Reference task IDs \
and concrete evidence. If something looks stuck or broken, say so directly.

You cannot modify harness state. If the operator needs to take action, suggest the \
specific harness CLI command they should run.

Current evidence is provided below. Answer from the evidence only. If the evidence \
is insufficient, say what additional query would help.\
"""


@dataclass(slots=True)
class AgentResponse:
    answer: str
    evidence_sources: list[str]
    error: str | None = None


class ObserverAgent:
    """Interrogation agent: receives questions, gathers evidence, queries LLM."""

    def __init__(
        self,
        query_client: HarnessQueryClient,
        evidence_cache: EvidenceCache,
        llm_command: str,
        project_id: str | None = None,
        llm_timeout: int = 120,
    ) -> None:
        self.query_client = query_client
        self.cache = evidence_cache
        self.llm_command = llm_command
        self.project_id = project_id
        self.llm_timeout = llm_timeout
        self._conversation: list[dict[str, str]] = []

    def ask(self, question: str) -> AgentResponse:
        """Answer a question by gathering evidence and consulting the LLM."""
        evidence, sources = self._gather_evidence(question)
        prompt = self._build_prompt(question, evidence)
        try:
            answer = self._invoke_llm(prompt)
        except Exception as exc:
            return AgentResponse(
                answer="",
                evidence_sources=sources,
                error=f"LLM invocation failed: {exc}",
            )
        self._conversation.append({"role": "user", "content": question})
        self._conversation.append({"role": "assistant", "content": answer})
        if len(self._conversation) > 20:
            self._conversation = self._conversation[-20:]
        return AgentResponse(answer=answer, evidence_sources=sources)

    def process_event(self, event: dict) -> str | None:
        """Process a pushed event and return a notification message if warranted."""
        event_type = event.get("event_type", "")
        notable = {
            "task_failed", "task_completed", "promotion_rejected",
            "promotion_approved", "branch_winner_selected", "run_blocked",
        }
        if event_type not in notable:
            return None
        entity_id = event.get("entity_id", "unknown")
        payload = event.get("payload", {})
        summary_parts = [f"Event: {event_type}"]
        if entity_id:
            summary_parts.append(f"Entity: {entity_id}")
        if payload:
            for key, value in list(payload.items())[:5]:
                summary_parts.append(f"  {key}: {value}")
        return "\n".join(summary_parts)

    def _gather_evidence(self, question: str) -> tuple[dict[str, object], list[str]]:
        """Fetch relevant evidence based on the question."""
        evidence: dict[str, object] = {}
        sources: list[str] = []
        lowered = question.lower()

        # Always fetch context packet for general awareness
        ctx = self.query_client.context_packet(self.project_id)
        if ctx.ok:
            evidence["context_packet"] = ctx.data
            self.cache.record("context_packet", ctx.data)
            sources.append("context-packet")

        # Ops report for backlog/stuck/blocked questions
        if any(word in lowered for word in ("backlog", "stuck", "blocked", "pending", "queue", "wait", "promotion", "affirm")):
            ops = self.query_client.ops_report(self.project_id)
            if ops.ok:
                evidence["ops_report"] = ops.data
                self.cache.record("ops_report", ops.data)
                sources.append("ops-report")

        # Task-specific queries when a task ID is mentioned
        task_id = self._extract_task_id(question)
        if task_id:
            task = self.query_client.task_report(task_id)
            if task.ok:
                evidence["task_report"] = task.data
                self.cache.record(f"task_report:{task_id}", task.data)
                sources.append(f"task-report:{task_id}")

        # Summary for broad questions
        if any(word in lowered for word in ("overview", "summary", "how many", "all project", "portfolio")):
            summary = self.query_client.summary(self.project_id)
            if summary.ok:
                evidence["summary"] = summary.data
                self.cache.record("summary", summary.data)
                sources.append("summary")

        # Events for history questions
        if any(word in lowered for word in ("happened", "history", "recent", "event", "what changed", "log")):
            events = self.query_client.events()
            if events.ok:
                evidence["events"] = events.data
                self.cache.record("events", events.data)
                sources.append("events")

        # Include diff if we have prior snapshots
        diff = self.cache.diff_latest("context_packet")
        if diff:
            evidence["changes_since_last_query"] = diff

        return evidence, sources

    def _build_prompt(self, question: str, evidence: dict) -> str:
        parts = [SYSTEM_PROMPT, ""]

        # Include recent conversation for context
        if self._conversation:
            parts.append("Recent conversation:")
            for msg in self._conversation[-6:]:
                role = "Operator" if msg["role"] == "user" else "Observer"
                parts.append(f"  {role}: {msg['content'][:500]}")
            parts.append("")

        parts.append("Evidence:")
        parts.append(json.dumps(evidence, indent=2, default=str))
        parts.append("")
        parts.append(f"Operator question: {question}")
        return "\n".join(parts)

    def _invoke_llm(self, prompt: str) -> str:
        completed = subprocess.run(
            self.llm_command,
            shell=True,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.llm_timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"LLM command failed (exit {completed.returncode}): {completed.stderr[:500]}")
        response = completed.stdout.strip()
        if not response:
            raise RuntimeError("LLM returned empty response")
        return response

    @staticmethod
    def _extract_task_id(text: str) -> str | None:
        """Extract a task ID (task_xxxx) from text if present."""
        for word in text.split():
            cleaned = word.strip(".,;:!?()\"'")
            if cleaned.startswith("task_") and len(cleaned) > 5:
                return cleaned
        return None
