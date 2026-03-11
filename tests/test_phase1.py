from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.config import HarnessConfig
from accruvia_harness.logging_utils import HarnessLogger, classify_error
from accruvia_harness.store import SQLiteHarnessStore
from accruvia_harness.telemetry import TelemetrySink


class Phase1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)

    def test_config_resolves_paths(self) -> None:
        config = HarnessConfig.from_env(
            db_path=self.base / "custom.db",
            workspace_root=self.base / "workspace",
            log_path=self.base / "logs" / "harness.jsonl",
        )
        self.assertEqual(self.base / "custom.db", config.db_path)
        self.assertEqual(self.base / "workspace", config.workspace_root)
        self.assertEqual(self.base / "logs" / "harness.jsonl", config.log_path)
        self.assertEqual("local", config.runtime_backend)
        self.assertEqual(1024, config.memory_limit_mb)
        self.assertEqual(300, config.cpu_time_limit_seconds)
        self.assertEqual("accruvia-harness", config.otel_service_name)
        self.assertIsNone(config.otel_exporter_otlp_endpoint)

    def test_logger_writes_jsonl(self) -> None:
        logger = HarnessLogger(self.base / "logs" / "harness.jsonl")
        logger.log("test_event", task_id="task_123")
        payload = json.loads((self.base / "logs" / "harness.jsonl").read_text(encoding="utf-8"))
        self.assertEqual("test_event", payload["event_type"])
        self.assertEqual("task_123", payload["task_id"])

    def test_error_classifier(self) -> None:
        self.assertEqual("validation_error", classify_error(ValueError("bad value")))
        self.assertEqual("unexpected_error", classify_error(RuntimeError("boom")))

    def test_store_reports_schema_version(self) -> None:
        store = SQLiteHarnessStore(self.base / "harness.db")
        store.initialize()
        self.assertEqual(store.expected_schema_version(), store.schema_version())

    def test_telemetry_summary_reports_cost_and_dashboard(self) -> None:
        telemetry = TelemetrySink(self.base / "telemetry")
        telemetry.metric("llm_cost_usd", 0.42, metric_type="histogram", llm_backend="codex")
        telemetry.metric("llm_total_tokens", 1234, llm_backend="codex")
        with telemetry.timed("work", validation_profile="python"):
            pass

        summary = telemetry.summary()

        self.assertFalse(summary["otel_enabled"])
        self.assertEqual(0.42, summary["cost_totals"]["cost_usd"])
        self.assertEqual(1234.0, summary["cost_totals"]["total_tokens"])
        self.assertTrue(summary["dashboard"]["slowest_operations_ms"])
