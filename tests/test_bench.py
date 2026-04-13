from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from accruvia_harness.bench import (
    ReviewStatus,
    decomposition_coverage,
    extract_mermaid_nodes,
    latest_review_status,
    ready_to_promote,
    task_counts_for_objective,
)
from accruvia_harness.store import SQLiteHarnessStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ExtractMermaidNodesTests(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual([], extract_mermaid_nodes(""))

    def test_parses_node_declarations(self) -> None:
        content = """flowchart TD
    O["Objective"]
    A["Step 1"]
    B["Step 2"]
    O --> A
    O --> B
"""
        self.assertEqual(["A", "B"], extract_mermaid_nodes(content))

    def test_filters_out_objective_node(self) -> None:
        content = 'flowchart TD\n    O["Obj"]\n    X["One"]'
        self.assertEqual(["X"], extract_mermaid_nodes(content))

    def test_deduplicates_repeat_declarations(self) -> None:
        content = """flowchart TD
    A["first"]
    A["second"]
    B["third"]
"""
        self.assertEqual(["A", "B"], extract_mermaid_nodes(content))


class BenchReviewStatusTests(unittest.TestCase):
    """ReviewStatus must derive review_clear from objective_review_completed
    + all objective_review_packet records carrying verdict=pass. The bench
    read this from a wrong record type before and never showed READY."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = Path(self.tmp.name) / "bench.db"
        self.store = SQLiteHarnessStore(db_path)
        self.store.initialize()
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

        # Minimal project + objective
        self.project_id = _new_id("project")
        self.conn.execute(
            """INSERT INTO projects (id, name, description, adapter_name,
                   workspace_policy, promotion_mode, repo_provider, repo_name,
                   base_branch, max_concurrent_tasks, created_at)
               VALUES (?, ?, '', 'generic', 'isolated_required', 'branch_and_pr',
                   'github', 'x/x', 'main', 1, ?)""",
            (self.project_id, f"p_{uuid.uuid4().hex[:6]}", _now()),
        )
        self.objective_id = _new_id("objective")
        self.conn.execute(
            """INSERT INTO objectives (id, project_id, title, summary, priority,
                   status, created_at, updated_at)
               VALUES (?, ?, 'Test', 'test', 100, 'open', ?, ?)""",
            (self.objective_id, self.project_id, _now(), _now()),
        )
        self.conn.commit()

    def _insert_context(
        self, record_type: str, metadata: dict, content: str = ""
    ) -> None:
        self.conn.execute(
            """INSERT INTO context_records (
                id, record_type, project_id, objective_id, task_id,
                visibility, author_type, author_id, content,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, NULL, 'model_visible', 'system', 'system', ?, ?, ?)""",
            (
                _new_id("context"),
                record_type,
                self.project_id,
                self.objective_id,
                content,
                json.dumps(metadata),
                _now(),
            ),
        )
        self.conn.commit()

    def test_no_review_started_returns_none_status(self) -> None:
        status = latest_review_status(self.conn, self.objective_id)
        self.assertEqual("none", status.status)
        self.assertFalse(status.review_clear)

    def test_started_without_completed_is_running(self) -> None:
        review_id = _new_id("objective_review")
        self._insert_context("objective_review_started", {"review_id": review_id})
        status = latest_review_status(self.conn, self.objective_id)
        self.assertEqual("running", status.status)
        self.assertEqual(review_id, status.review_id)
        self.assertFalse(status.review_clear)

    def test_completed_with_all_pass_packets_is_clear(self) -> None:
        review_id = _new_id("objective_review")
        self._insert_context("objective_review_started", {"review_id": review_id})
        for _ in range(3):
            self._insert_context(
                "objective_review_packet",
                {"review_id": review_id, "verdict": "pass"},
            )
        self._insert_context(
            "objective_review_completed",
            {"review_id": review_id, "packet_count": 3},
        )
        status = latest_review_status(self.conn, self.objective_id)
        self.assertEqual("completed", status.status)
        self.assertEqual(3, status.packet_count)
        self.assertEqual(3, status.pass_count)
        self.assertEqual(0, status.fail_count)
        self.assertTrue(status.review_clear)

    def test_completed_with_one_fail_packet_is_not_clear(self) -> None:
        review_id = _new_id("objective_review")
        self._insert_context("objective_review_started", {"review_id": review_id})
        self._insert_context(
            "objective_review_packet",
            {"review_id": review_id, "verdict": "pass"},
        )
        self._insert_context(
            "objective_review_packet",
            {"review_id": review_id, "verdict": "fail"},
        )
        self._insert_context(
            "objective_review_completed",
            {"review_id": review_id, "packet_count": 2},
        )
        status = latest_review_status(self.conn, self.objective_id)
        self.assertFalse(status.review_clear)
        self.assertEqual(1, status.fail_count)

    def test_packets_without_review_id_still_counted(self) -> None:
        """Existing production harness writes packets with no review_id on the
        packet metadata. The bench must still be able to associate them with
        the enclosing review round using created_at windowing."""
        review_id = _new_id("objective_review")
        self._insert_context("objective_review_started", {"review_id": review_id})
        # Packets with NO review_id in metadata
        self._insert_context("objective_review_packet", {"verdict": "pass"})
        self._insert_context("objective_review_packet", {"verdict": "pass"})
        self._insert_context(
            "objective_review_completed",
            {"review_id": review_id, "packet_count": 2},
        )
        status = latest_review_status(self.conn, self.objective_id)
        self.assertEqual("completed", status.status)
        self.assertEqual(2, status.packet_count)
        self.assertTrue(status.review_clear)


class ReadyToPromoteTests(unittest.TestCase):
    def _review(self, status: str = "completed", clear: bool = True, passes: int = 1, fails: int = 0) -> ReviewStatus:
        return ReviewStatus(
            review_id="objective_review_x" if status != "none" else None,
            status=status,
            packet_count=passes + fails,
            pass_count=passes,
            fail_count=fails,
            review_clear=clear,
            completed_at="2026-04-12T00:00:00+00:00" if status == "completed" else None,
        )

    def _coverage(self, total: int = 2, with_tasks: int = 2, completed: int = 2) -> dict:
        return {
            "total_nodes": total,
            "nodes_with_plans": with_tasks,
            "nodes_with_tasks": with_tasks,
            "nodes_completed": completed,
            "gaps": [],
            "orphan_tasks": [],
        }

    def _counts(self, **kwargs) -> dict[str, int]:
        base = {"pending": 0, "active": 0, "completed": 2, "failed": 0, "total": 2}
        base.update(kwargs)
        return base

    def test_ready_when_all_green(self) -> None:
        ok, reason = ready_to_promote(self._counts(), self._review(), self._coverage())
        self.assertTrue(ok, reason)
        self.assertEqual("clear", reason)

    def test_blocked_when_tasks_pending(self) -> None:
        ok, reason = ready_to_promote(
            self._counts(pending=1, completed=1, total=2),
            self._review(),
            self._coverage(),
        )
        self.assertFalse(ok)
        self.assertIn("in flight", reason)

    def test_blocked_when_task_failed(self) -> None:
        ok, reason = ready_to_promote(
            self._counts(failed=1, completed=1, total=2),
            self._review(),
            self._coverage(),
        )
        self.assertFalse(ok)
        self.assertIn("failed", reason)

    def test_blocked_when_review_not_clear(self) -> None:
        ok, reason = ready_to_promote(
            self._counts(), self._review(clear=False, passes=1, fails=1), self._coverage()
        )
        self.assertFalse(ok)
        self.assertIn("blocked", reason)

    def test_blocked_when_review_never_started(self) -> None:
        ok, reason = ready_to_promote(
            self._counts(), self._review(status="none"), self._coverage()
        )
        self.assertFalse(ok)
        self.assertIn("no review", reason)

    def test_blocked_when_decomp_incomplete(self) -> None:
        ok, reason = ready_to_promote(
            self._counts(), self._review(), self._coverage(total=3, with_tasks=2, completed=2)
        )
        self.assertFalse(ok)
        self.assertIn("decomp", reason)

    def test_blocked_when_no_tasks(self) -> None:
        ok, reason = ready_to_promote(
            {"pending": 0, "active": 0, "completed": 0, "failed": 0, "total": 0},
            self._review(),
            self._coverage(total=0, with_tasks=0, completed=0),
        )
        self.assertFalse(ok)
        self.assertEqual("no tasks", reason)


if __name__ == "__main__":
    unittest.main()
