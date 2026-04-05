from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.cost_tracker import CostTracker


class CostTrackerTests(unittest.TestCase):
    def test_record_run_cost_reads_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "runs" / "run_001"
            run_dir.mkdir(parents=True)
            (run_dir / "llm_metadata.json").write_text(
                json.dumps({"cost_usd": 1.25, "model": "test-model", "prompt_tokens": 100}),
                encoding="utf-8",
            )

            tracker = CostTracker(ledger_path=tmp_path / "cost_ledger.json")
            cost = tracker.record_run_cost("proj-1", "run_001", run_dir)

            self.assertEqual(cost, 1.25)
            # Verify the ledger file was written with the correct total
            ledger = json.loads((tmp_path / "cost_ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ledger), 1)
            self.assertAlmostEqual(list(ledger.values())[0], 1.25)

    def test_daily_cost_accumulates_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tracker = CostTracker(ledger_path=tmp_path / "cost_ledger.json")

            for i, cost_val in enumerate([0.50, 0.75, 1.00]):
                run_dir = tmp_path / "runs" / f"run_{i}"
                run_dir.mkdir(parents=True)
                (run_dir / "llm_metadata.json").write_text(
                    json.dumps({"cost_usd": cost_val}),
                    encoding="utf-8",
                )
                tracker.record_run_cost("proj-1", f"run_{i}", run_dir)

            total = tracker.daily_cost("proj-1")
            self.assertAlmostEqual(total, 2.25)

    def test_check_budget_returns_false_when_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tracker = CostTracker(ledger_path=tmp_path / "cost_ledger.json")

            run_dir = tmp_path / "runs" / "run_big"
            run_dir.mkdir(parents=True)
            (run_dir / "llm_metadata.json").write_text(
                json.dumps({"cost_usd": 25.0}),
                encoding="utf-8",
            )
            tracker.record_run_cost("proj-1", "run_big", run_dir)

            within, remaining = tracker.check_budget("proj-1", daily_limit_usd=20.0)
            self.assertFalse(within)
            self.assertAlmostEqual(remaining, -5.0)

    def test_record_run_cost_missing_metadata_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tracker = CostTracker(ledger_path=tmp_path / "cost_ledger.json")

            run_dir = tmp_path / "runs" / "run_empty"
            run_dir.mkdir(parents=True)
            # No llm_metadata.json — should return 0.0 gracefully

            cost = tracker.record_run_cost("proj-1", "run_empty", run_dir)
            self.assertEqual(cost, 0.0)


if __name__ == "__main__":
    unittest.main()
