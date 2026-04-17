"""Repository for objective review packets.

Single authoritative location for reading and writing review packets
from/to ContextRecord storage. Replaces the 6+ scattered query patterns
in the old ObjectiveReviewMixin.
"""
from __future__ import annotations

from typing import Any

from ..domain import (
    ContextRecord,
    ReviewPacket,
    ReviewRound,
    ReviewVerdict,
    new_id,
)


class ReviewPacketRepository:
    """Read/write review packets via ContextRecord storage."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def save_packets(
        self,
        objective_id: str,
        project_id: str,
        review_id: str,
        packets: list[ReviewPacket | dict[str, Any]],
    ) -> list[str]:
        """Persist packets as context records. Returns list of record IDs."""
        record_ids: list[str] = []
        for packet in packets:
            if isinstance(packet, ReviewPacket):
                d = packet.to_dict()
            else:
                d = dict(packet)
            record = ContextRecord(
                id=new_id("context"),
                record_type="objective_review_packet",
                project_id=project_id,
                objective_id=objective_id,
                visibility="operator_visible",
                author_type="system",
                content=str(d.get("summary") or ""),
                metadata={
                    "review_id": review_id,
                    "reviewer": d.get("reviewer", ""),
                    "dimension": d.get("dimension", ""),
                    "verdict": d.get("verdict", ""),
                    "progress_status": d.get("progress_status"),
                    "severity": d.get("severity"),
                    "owner_scope": d.get("owner_scope"),
                    "findings": d.get("findings", []),
                    "evidence": d.get("evidence", []),
                    "required_artifact_type": d.get("required_artifact_type"),
                    "artifact_schema": d.get("artifact_schema"),
                    "evidence_contract": d.get("evidence_contract"),
                    "closure_criteria": d.get("closure_criteria"),
                    "evidence_required": d.get("evidence_required"),
                    "repeat_reason": d.get("repeat_reason"),
                    "llm_usage": d.get("llm_usage"),
                    "llm_usage_reported": d.get("llm_usage_reported"),
                    "llm_usage_source": d.get("llm_usage_source"),
                    "backend": d.get("backend"),
                    "prompt_path": d.get("prompt_path"),
                    "response_path": d.get("response_path"),
                    "review_task_id": d.get("review_task_id"),
                    "review_run_id": d.get("review_run_id"),
                },
            )
            self.store.create_context_record(record)
            record_ids.append(record.id)
        return record_ids

    def load_packets_for_review(
        self, objective_id: str, review_id: str,
    ) -> list[ReviewPacket]:
        """Load all packets for a specific review round."""
        records = self.store.list_context_records(
            objective_id=objective_id, record_type="objective_review_packet",
        )
        packets: list[ReviewPacket] = []
        for record in records:
            meta = record.metadata if isinstance(record.metadata, dict) else {}
            if str(meta.get("review_id") or "") != review_id:
                continue
            p = ReviewPacket.from_dict(meta)
            p.packet_record_id = record.id
            packets.append(p)
        return packets

    def load_round(
        self, objective_id: str, review_id: str,
    ) -> ReviewRound:
        """Load a ReviewRound for a specific review."""
        packets = self.load_packets_for_review(objective_id, review_id)
        return ReviewRound(
            review_id=review_id,
            objective_id=objective_id,
            packets=packets,
        )

    def latest_round(self, objective_id: str) -> ReviewRound | None:
        """Load the most recent review round, or None if no reviews exist."""
        records = self.store.list_context_records(
            objective_id=objective_id, record_type="objective_review_packet",
        )
        if not records:
            return None
        latest_review_id = ""
        for record in reversed(records):
            meta = record.metadata if isinstance(record.metadata, dict) else {}
            rid = str(meta.get("review_id") or "").strip()
            if rid:
                latest_review_id = rid
                break
        if not latest_review_id:
            return None
        return self.load_round(objective_id, latest_review_id)

    def all_review_ids(self, objective_id: str) -> list[str]:
        """Return all review IDs for an objective, newest first."""
        records = self.store.list_context_records(
            objective_id=objective_id, record_type="objective_review_packet",
        )
        seen: dict[str, bool] = {}
        ids: list[str] = []
        for record in reversed(records):
            meta = record.metadata if isinstance(record.metadata, dict) else {}
            rid = str(meta.get("review_id") or "").strip()
            if rid and rid not in seen:
                seen[rid] = True
                ids.append(rid)
        return ids
