from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.config import HarnessConfig
from accruvia_harness.commands.core import _redact_command
from accruvia_harness.logging_utils import HarnessLogger, classify_error
from accruvia_harness.store import SQLiteHarnessStore
from accruvia_harness.telemetry import TelemetrySink, _otlp_signal_endpoints


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
        self.assertEqual((), config.env_passthrough)

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

    def test_config_reads_env_passthrough(self) -> None:
        original = os.environ.get("ACCRUVIA_ENV_PASSTHROUGH")
        self.addCleanup(
            lambda: os.environ.__setitem__("ACCRUVIA_ENV_PASSTHROUGH", original)
            if original is not None
            else os.environ.pop("ACCRUVIA_ENV_PASSTHROUGH", None)
        )
        os.environ["ACCRUVIA_ENV_PASSTHROUGH"] = "FOO,BAR"

        config = HarnessConfig.from_env()

        self.assertEqual(("FOO", "BAR"), config.env_passthrough)

    def test_config_invalid_numeric_envs_fall_back_to_defaults(self) -> None:
        env = {
            "ACCRUVIA_TIMEOUT_EMA_ALPHA": "abc",
            "ACCRUVIA_TIMEOUT_MIN_SECONDS": "oops",
            "ACCRUVIA_MEMORY_LIMIT_MB": "nah",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            config = HarnessConfig.from_env()

        self.assertEqual(0.5, config.timeout_ema_alpha)
        self.assertEqual(30, config.timeout_min_seconds)
        self.assertEqual(1024, config.memory_limit_mb)

    def test_config_payload_round_trip_preserves_runtime_settings(self) -> None:
        config = HarnessConfig.from_env(
            db_path=self.base / "payload.db",
            workspace_root=self.base / "workspace",
            log_path=self.base / "harness.log",
        )
        payload = config.to_payload()

        restored = HarnessConfig.from_payload(payload)

        self.assertEqual(config.db_path, restored.db_path)
        self.assertEqual(config.workspace_root, restored.workspace_root)
        self.assertEqual(config.runtime_backend, restored.runtime_backend)
        self.assertEqual(config.timeout_multiplier, restored.timeout_multiplier)

    def test_redact_command_hides_command_tail(self) -> None:
        self.assertEqual("codex [REDACTED]", _redact_command("codex exec --api-key secret"))
        self.assertIsNone(_redact_command(None))

    def test_otlp_signal_endpoints_are_derived_per_signal(self) -> None:
        self.assertEqual(
            ("http://localhost:4318/v1/traces", "http://localhost:4318/v1/metrics"),
            _otlp_signal_endpoints("http://localhost:4318"),
        )
        self.assertEqual(
            ("http://localhost:4318/v1/traces", "http://localhost:4318/v1/metrics"),
            _otlp_signal_endpoints("http://localhost:4318/v1/traces"),
        )

    def test_telemetry_summary_surfaces_otel_setup_warning(self) -> None:
        with unittest.mock.patch(
            "accruvia_harness.telemetry._OpenTelemetryBridge.build",
            return_value=(None, "error", "OpenTelemetry import/setup failed: boom"),
        ):
            telemetry = TelemetrySink(self.base / "telemetry", otlp_endpoint="http://localhost:4318")

        summary = telemetry.summary()

        self.assertEqual("error", summary["otel_status"])
        self.assertEqual("OpenTelemetry import/setup failed: boom", summary["otel_warning"])
        self.assertEqual("otel_setup", summary["warnings"][0]["category"])

    def test_telemetry_load_skips_corrupt_jsonl_lines(self) -> None:
        telemetry = TelemetrySink(self.base / "telemetry")
        telemetry.metrics_path.write_text('{"name":"ok","value":1}\nnot-json\n', encoding="utf-8")

        metrics = telemetry.load_metrics()

        self.assertEqual(1, len(metrics))
        self.assertEqual("ok", metrics[0]["name"])

    def test_telemetry_negative_after_positive_counter_is_ignored_with_warning(self) -> None:
        telemetry = TelemetrySink(self.base / "telemetry", otlp_endpoint="http://localhost:4318")
        class FakeOtel:
            trace_endpoint = "http://localhost:4318/v1/traces"
            metric_endpoint = "http://localhost:4318/v1/metrics"
            def __init__(self):
                self.values = []
            def metric(self, name, value, metric_type, attributes):
                if not hasattr(self, "seen"):
                    self.seen = set()
                if name not in self.seen and value >= 0:
                    self.seen.add(name)
                    return
                if value < 0:
                    raise ValueError("negative monotonic counter")
            def span(self, name, duration_ms, attributes):
                return None
        telemetry._otel = FakeOtel()
        telemetry.otel_status = "enabled"

        telemetry.metric("queue_delta", 1)
        telemetry.metric("queue_delta", -1)
        summary = telemetry.summary()

        self.assertTrue(any(item["category"] == "otel_metric_export" for item in summary["warnings"]))
