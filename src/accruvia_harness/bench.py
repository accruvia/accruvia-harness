"""Objective bench library.

Reads harness DB state and computes per-objective verification signals:
task counts, decomposition coverage against mermaid, latest objective
review status, READY-to-promote signal.

Used by bin/accruvia-objective-bench (thin CLI wrapper) and tested
directly in tests/test_bench.py.

This module is a query-only library — it must never write to the DB.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

_NODE_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\[')


def extract_mermaid_nodes(content: str) -> list[str]:
    """Parse node ids from a mermaid flowchart. Returns ids in declaration order.

    A node declaration looks like 'T_abc123["label"]' at the start of a
    line. Edges are ignored. The objective's own 'O' node is filtered
    out — we only care about decomposition children.
    """
    if not content:
        return []
    nodes: list[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        m = _NODE_RE.match(line)
        if not m:
            continue
        node = m.group(1)
        if node == "O" or node in seen:
            continue
        seen.add(node)
        nodes.append(node)
    return nodes


@dataclass(frozen=True, slots=True)
class ReviewStatus:
    """Latest objective-review state derived from context_records.

    The harness writes three record types during a review round:
      - ``objective_review_started`` (opens the round)
      - ``objective_review_packet`` (one per reviewer, carries verdict)
      - ``objective_review_completed`` (closes the round)

    ``review_clear`` is True only when ``objective_review_completed``
    exists for the latest review_id AND every packet under that review
    carries verdict=``pass``. Any non-pass verdict keeps the review
    blocked.
    """
    review_id: str | None
    status: str  # "none" | "running" | "completed"
    packet_count: int
    pass_count: int
    fail_count: int
    review_clear: bool
    completed_at: str | None


def latest_review_status(conn: sqlite3.Connection, objective_id: str) -> ReviewStatus:
    """Compute ReviewStatus for the latest review round on an objective."""
    started = conn.execute(
        """
        SELECT metadata_json, created_at FROM context_records
         WHERE objective_id = ? AND record_type = 'objective_review_started'
         ORDER BY created_at DESC LIMIT 1
        """,
        (objective_id,),
    ).fetchone()
    if started is None:
        return ReviewStatus(None, "none", 0, 0, 0, False, None)
    try:
        meta = json.loads(started["metadata_json"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    review_id = meta.get("review_id")
    if not review_id:
        return ReviewStatus(None, "none", 0, 0, 0, False, None)

    completed = conn.execute(
        """
        SELECT metadata_json, created_at FROM context_records
         WHERE objective_id = ? AND record_type = 'objective_review_completed'
         ORDER BY created_at DESC LIMIT 1
        """,
        (objective_id,),
    ).fetchone()

    if completed is not None:
        try:
            completed_meta = json.loads(completed["metadata_json"] or "{}")
        except json.JSONDecodeError:
            completed_meta = {}
        if completed_meta.get("review_id") != review_id:
            completed = None

    # Count packets for THIS review_id
    packet_rows = conn.execute(
        """
        SELECT metadata_json FROM context_records
         WHERE objective_id = ? AND record_type = 'objective_review_packet'
         ORDER BY created_at DESC
        """,
        (objective_id,),
    ).fetchall()

    packets_for_round: list[dict] = []
    for row in packet_rows:
        try:
            pm = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if pm.get("review_id") == review_id or (
            completed is not None and pm.get("review_id") is None
        ):
            packets_for_round.append(pm)

    # Fallback: if no packet had a matching review_id and there are packets newer
    # than the started event, associate them with the current round. This
    # handles the existing data shape where packets do not carry review_id.
    if not packets_for_round and completed is not None:
        recent_packets = conn.execute(
            """
            SELECT metadata_json FROM context_records
             WHERE objective_id = ? AND record_type = 'objective_review_packet'
               AND created_at >= ? AND created_at <= ?
            """,
            (objective_id, started["created_at"], completed["created_at"]),
        ).fetchall()
        for row in recent_packets:
            try:
                packets_for_round.append(json.loads(row["metadata_json"] or "{}"))
            except json.JSONDecodeError:
                continue

    pass_count = sum(1 for p in packets_for_round if str(p.get("verdict", "")).lower() == "pass")
    fail_count = sum(1 for p in packets_for_round if str(p.get("verdict", "")).lower() not in ("pass", ""))
    packet_count = len(packets_for_round)

    if completed is None:
        return ReviewStatus(
            review_id=review_id,
            status="running",
            packet_count=packet_count,
            pass_count=pass_count,
            fail_count=fail_count,
            review_clear=False,
            completed_at=None,
        )

    review_clear = bool(packet_count > 0 and fail_count == 0)
    return ReviewStatus(
        review_id=review_id,
        status="completed",
        packet_count=packet_count,
        pass_count=pass_count,
        fail_count=fail_count,
        review_clear=review_clear,
        completed_at=completed["created_at"],
    )


def task_counts_for_objective(conn: sqlite3.Connection, objective_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM tasks WHERE objective_id = ? GROUP BY status",
        (objective_id,),
    ).fetchall()
    counts = {"pending": 0, "active": 0, "completed": 0, "failed": 0}
    for r in rows:
        counts[r["status"]] = r["c"]
    counts["total"] = sum(counts.values())
    return counts


def decomposition_coverage(conn: sqlite3.Connection, objective_id: str) -> dict[str, Any]:
    """Compare mermaid node set to task set."""
    row = conn.execute(
        """
        SELECT content FROM mermaid_artifacts
         WHERE objective_id = ? AND diagram_type = 'workflow_control'
         ORDER BY version DESC LIMIT 1
        """,
        (objective_id,),
    ).fetchone()
    nodes = extract_mermaid_nodes(row["content"] if row else "")
    node_set = set(nodes)

    task_rows = conn.execute(
        "SELECT id, mermaid_node_id, status FROM tasks WHERE objective_id = ?",
        (objective_id,),
    ).fetchall()
    plan_rows = conn.execute(
        "SELECT mermaid_node_id FROM plans WHERE objective_id = ? AND mermaid_node_id IS NOT NULL",
        (objective_id,),
    ).fetchall()

    nodes_with_tasks = {t["mermaid_node_id"] for t in task_rows if t["mermaid_node_id"]}
    nodes_completed = {
        t["mermaid_node_id"] for t in task_rows
        if t["mermaid_node_id"] and t["status"] == "completed"
    }
    nodes_with_plans = {p["mermaid_node_id"] for p in plan_rows if p["mermaid_node_id"]}

    gaps = [n for n in nodes if n not in nodes_with_tasks]
    orphan_tasks = [
        t["id"] for t in task_rows
        if t["mermaid_node_id"] and t["mermaid_node_id"] not in node_set
    ]

    return {
        "total_nodes": len(nodes),
        "nodes_with_plans": len(nodes_with_plans & node_set) if node_set else len(nodes_with_plans),
        "nodes_with_tasks": len(nodes_with_tasks & node_set),
        "nodes_completed": len(nodes_completed & node_set),
        "gaps": gaps,
        "orphan_tasks": orphan_tasks,
    }


def ready_to_promote(
    counts: dict[str, int],
    review: ReviewStatus,
    coverage: dict[str, Any],
) -> tuple[bool, str]:
    """Return (is_ready, reason). An objective is READY only when:
      - It has at least one task
      - No tasks pending or active
      - No tasks failed
      - The mermaid decomposition is fully covered (every node has a completed task)
      - The latest objective review is completed and all packets passed
    """
    if counts["total"] == 0:
        return False, "no tasks"
    if counts["pending"] or counts["active"]:
        return False, f"{counts['pending']}p/{counts['active']}a still in flight"
    if counts["failed"]:
        return False, f"{counts['failed']} failed"
    if coverage["total_nodes"] > 0 and coverage["nodes_completed"] < coverage["total_nodes"]:
        return False, f"decomp {coverage['nodes_completed']}/{coverage['total_nodes']}"
    if review.status == "none":
        return False, "no review round recorded"
    if review.status == "running":
        return False, f"review running ({review.pass_count}p/{review.fail_count}f so far)"
    if not review.review_clear:
        return False, f"review blocked ({review.fail_count} non-pass packet(s))"
    return True, "clear"


def mermaid_for_objective(conn: sqlite3.Connection, objective_id: str) -> dict | None:
    r = conn.execute(
        """
        SELECT id, diagram_type, status, version, required_for_execution,
               LENGTH(content) AS clen
          FROM mermaid_artifacts
         WHERE objective_id = ?
         ORDER BY version DESC
         LIMIT 1
        """,
        (objective_id,),
    ).fetchone()
    return dict(r) if r is not None else None
