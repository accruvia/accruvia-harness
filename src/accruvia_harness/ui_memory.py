from __future__ import annotations

import re

from .ui_responder import RetrievedMemory


class LocalContextMemoryProvider:
    """Deterministic fallback retrieval until Open Brain is wired in."""

    _TOKEN_RE = re.compile(r"[a-z0-9_]+")

    def __init__(self, store) -> None:
        self.store = store

    def retrieve(self, *, project_id: str, objective_id: str | None, query_text: str, limit: int = 3) -> list[RetrievedMemory]:
        query_tokens = set(self._tokens(query_text))
        if not query_tokens:
            return []
        candidates = []
        for record in self.store.list_context_records(project_id=project_id, objective_id=objective_id):
            if record.record_type not in {
                "operator_comment",
                "operator_frustration",
                "harness_reply",
                "mermaid_status_change",
                "task_created",
            }:
                continue
            haystack = f"{record.record_type} {record.content}".lower()
            score = len(query_tokens.intersection(self._tokens(haystack)))
            if score <= 0:
                continue
            candidates.append(
                (
                    score,
                    record.created_at,
                    RetrievedMemory(summary=record.content[:280], source=record.record_type),
                )
            )
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in candidates[:limit]]

    def _tokens(self, text: str) -> list[str]:
        return self._TOKEN_RE.findall(text.lower())
