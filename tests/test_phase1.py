from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.config import HarnessConfig
from accruvia_harness.logging_utils import HarnessLogger, classify_error
from accruvia_harness.store import SQLiteHarnessStore


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
