from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.control_breadcrumbs import BreadcrumbWriter
from accruvia_harness.control_classifier import FailureClassifier
from accruvia_harness.store import SQLiteHarnessStore


class FailureClassifierTests(unittest.TestCase):
    def test_classifies_rate_limit_without_retry(self) -> None:
        result = FailureClassifier().classify("API rate limit reached. Provider returned 429.")

        self.assertEqual("provider_rate_limit", result.classification)
        self.assertFalse(result.retry_recommended)
        self.assertEqual(1800, result.cooldown_seconds)

    def test_classifies_timeout_as_retryable(self) -> None:
        result = FailureClassifier().classify("Worker timed out after 1800 seconds.")

        self.assertEqual("timeout", result.classification)
        self.assertTrue(result.retry_recommended)


class BreadcrumbWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace_root = root / "workspace"
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()

    def test_writes_bundle_and_indexes_it(self) -> None:
        writer = BreadcrumbWriter(self.store, self.workspace_root)

        bundle_dir = writer.write_bundle(
            entity_type="task",
            entity_id="task_123",
            meta={"task_id": "task_123"},
            evidence={"checks": [{"name": "tests", "result": "pass"}]},
            decision={"classification": "timeout", "retry_recommended": True},
            worker_run_id="run_123",
            summary="Tests passed but worker timed out after validation.",
        )

        self.assertTrue((bundle_dir / "meta.json").exists())
        self.assertTrue((bundle_dir / "evidence.json").exists())
        self.assertTrue((bundle_dir / "decision.json").exists())
        self.assertTrue((bundle_dir / "summary.txt").exists())

        indexed = self.store.list_control_breadcrumbs(entity_type="task", entity_id="task_123")
        self.assertEqual(1, len(indexed))
        self.assertEqual("run_123", indexed[0].worker_run_id)
        self.assertEqual("timeout", indexed[0].classification)
