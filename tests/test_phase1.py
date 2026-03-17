from __future__ import annotations

import json
import io
import multiprocessing
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from accruvia_harness.config import HarnessConfig
from accruvia_harness.commands.core import _build_supervise_restart_command, _emit_supervise_progress, _redact_command
from accruvia_harness.cli_parser import build_parser
from accruvia_harness.logging_utils import HarnessLogger, classify_error
from accruvia_harness.store import SQLiteHarnessStore
from accruvia_harness.telemetry import TelemetrySink, _otlp_signal_endpoints
from accruvia_harness.timeout_policy import ExecutionTimeoutPolicy


def _write_telemetry_metric(root: str, prefix: str, count: int) -> None:
    telemetry = TelemetrySink(Path(root))
    for index in range(count):
        telemetry.metric(f"{prefix}_{index}", index + 1)


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
        self.assertEqual(1800, config.task_run_timeout_seconds)
        self.assertEqual(1800, config.task_llm_timeout_seconds)
        self.assertEqual(300, config.task_validation_timeout_seconds)
        self.assertEqual(30, config.task_validation_startup_timeout_seconds)
        self.assertEqual(120, config.task_compile_timeout_seconds)
        self.assertEqual(30, config.task_git_timeout_seconds)
        self.assertEqual(300, config.task_stale_timeout_seconds)
        self.assertFalse(config.telemetry_fsync_writes)
        self.assertEqual("accruvia-harness", config.otel_service_name)
        self.assertIsNone(config.otel_exporter_otlp_endpoint)
        self.assertEqual((), config.env_passthrough)
        self.assertEqual(3, config.heartbeat_failure_escalation_threshold)
        self.assertTrue(config.pr_check_enabled)
        self.assertEqual(28800, config.pr_check_interval_seconds)

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
            "ACCRUVIA_TASK_RUN_TIMEOUT_SECONDS": "later",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            config = HarnessConfig.from_env()

        self.assertEqual(0.5, config.timeout_ema_alpha)
        self.assertEqual(30, config.timeout_min_seconds)
        self.assertEqual(1024, config.memory_limit_mb)
        self.assertEqual(1800, config.task_run_timeout_seconds)

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
        self.assertEqual(config.telemetry_fsync_writes, restored.telemetry_fsync_writes)
        self.assertEqual(
            config.heartbeat_failure_escalation_threshold,
            restored.heartbeat_failure_escalation_threshold,
        )
        self.assertEqual(config.task_run_timeout_seconds, restored.task_run_timeout_seconds)
        self.assertEqual(config.task_llm_timeout_seconds, restored.task_llm_timeout_seconds)
        self.assertEqual(
            config.task_validation_startup_timeout_seconds,
            restored.task_validation_startup_timeout_seconds,
        )
        self.assertEqual(config.pr_check_enabled, restored.pr_check_enabled)
        self.assertEqual(config.pr_check_interval_seconds, restored.pr_check_interval_seconds)

    def test_timeout_policy_tightens_after_repeated_timeout_failures(self) -> None:
        telemetry = TelemetrySink(self.base / "telemetry")
        telemetry.span("work", duration_ms=100000, validation_profile="generic", worker_backend="agent")
        telemetry.span("work", duration_ms=120000, validation_profile="generic", worker_backend="agent")
        telemetry.warn("validation_timeout", "timed out", validation_profile="generic", worker_backend="agent")
        telemetry.warn("validation_timeout", "timed out", validation_profile="generic", worker_backend="agent")
        telemetry.warn("validation_timeout", "timed out", validation_profile="generic", worker_backend="agent")

        policy = ExecutionTimeoutPolicy(telemetry, min_seconds=30, max_seconds=1200, multiplier=2.5)

        self.assertEqual(policy.timeout_seconds("generic", "agent"), 1200)

    def test_config_reads_telemetry_fsync_flag(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ACCRUVIA_TELEMETRY_FSYNC_WRITES": "true"},
            clear=False,
        ):
            config = HarnessConfig.from_env()

        self.assertTrue(config.telemetry_fsync_writes)

    def test_redact_command_hides_command_tail(self) -> None:
        self.assertEqual("codex [REDACTED]", _redact_command("codex exec --api-key secret"))
        self.assertIsNone(_redact_command(None))

    def test_build_supervise_restart_command_preserves_runtime_options(self) -> None:
        command = _build_supervise_restart_command(
            {
                "project_id": "project_123",
                "worker_id": "babysitter",
                "lease_seconds": 300,
                "watch": True,
                "idle_sleep_seconds": 15.0,
                "max_idle_cycles": 4,
                "max_iterations": 20,
                "heartbeat_project_ids": ["project_123"],
                "heartbeat_interval_seconds": 60.0,
                "heartbeat_all_projects": False,
                "review_check_enabled": True,
                "review_check_interval_seconds": 7200,
            }
        )

        self.assertIn("supervise", command)
        self.assertIn("--project-id", command)
        self.assertIn("--worker-id", command)
        self.assertIn("--heartbeat-project-id", command)
        self.assertIn("--review-check-enabled", command)

    def test_supervise_cli_defaults_to_watch_mode(self) -> None:
        args = build_parser().parse_args(["supervise"])

        self.assertTrue(args.watch)

    def test_build_supervise_restart_command_uses_one_shot_for_non_watch_mode(self) -> None:
        command = _build_supervise_restart_command({"watch": False})

        self.assertIn("--one-shot", command)

    def test_supervise_progress_prints_atomicity_follow_on_and_retry_reset(self) -> None:
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            _emit_supervise_progress(
                {
                    "type": "atomicity_follow_on_created",
                    "task_id": "task_parent",
                    "task_title": "Tighten operator scope",
                    "follow_on_task_id": "task_child",
                    "follow_on_title": "Tighten operator scope: narrower atomic slice",
                    "failure_category": "policy_self_modification",
                }
            )
            _emit_supervise_progress(
                {
                    "type": "queue_retry_cycle_reset",
                    "queue_depth": 2,
                }
            )

        output = stdout.getvalue()
        self.assertIn("Atomicity split queued for Tighten operator scope (task_parent)", output)
        self.assertIn("Follow-on: Tighten operator scope: narrower atomic slice (task_child)", output)
        self.assertIn("Reason: policy_self_modification", output)
        self.assertIn("Retryable tasks still pending; starting another sweep of 2 queued tasks", output)

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
        with mock.patch(
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

    def test_telemetry_supports_multi_process_appends(self) -> None:
        telemetry_root = self.base / "telemetry"
        ctx = multiprocessing.get_context("spawn")
        first = ctx.Process(target=_write_telemetry_metric, args=(str(telemetry_root), "a", 20))
        second = ctx.Process(target=_write_telemetry_metric, args=(str(telemetry_root), "b", 20))

        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)

        self.assertEqual(0, first.exitcode)
        self.assertEqual(0, second.exitcode)

        telemetry = TelemetrySink(telemetry_root)
        metrics = telemetry.load_metrics()

        self.assertEqual(40, len(metrics))
        self.assertEqual(40, len({item["name"] for item in metrics}))

    def test_telemetry_replays_journal_after_partial_crash(self) -> None:
        telemetry = TelemetrySink(self.base / "telemetry")
        envelope = {
            "sequence": 1,
            "record_id": "telemetry_test",
            "channel": "metric",
            "payload": {
                "timestamp": "2026-03-11T00:00:00+00:00",
                "type": "metric",
                "name": "replayed_metric",
                "metric_type": "counter",
                "value": 3,
                "attributes": {},
            },
        }
        telemetry.journal_path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
        telemetry.state_path.write_text(
            json.dumps({"last_sequence": 1, "last_materialized_sequence": 0}),
            encoding="utf-8",
        )

        metrics = telemetry.load_metrics()
        summary = telemetry.summary()

        self.assertEqual(1, len([item for item in metrics if item["name"] == "replayed_metric"]))
        self.assertEqual(0, summary["journal_backlog"])
        self.assertEqual(1, summary["last_materialized_sequence"])

    def test_telemetry_summary_reports_journal_paths(self) -> None:
        telemetry = TelemetrySink(self.base / "telemetry")

        summary = telemetry.summary()

        self.assertEqual(str(telemetry.journal_path), summary["journal_path"])
        self.assertEqual(str(telemetry.state_path), summary["telemetry_state_path"])

    def test_store_enables_sqlite_wal_and_busy_timeout(self) -> None:
        store = SQLiteHarnessStore(self.base / "harness.db")
        store.initialize()

        with store.connect() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertEqual("wal", str(journal_mode).lower())
        self.assertEqual(30000, int(busy_timeout))
