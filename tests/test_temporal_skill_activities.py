from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import Project, Task, TaskStatus, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.store import SQLiteHarnessStore
from accruvia_harness.temporal_backend import (
    task_to_stable_activity,
    process_next_task_activity,
)
from accruvia_harness.workers import LocalArtifactWorker


def _minimal_config(base: Path) -> HarnessConfig:
    return HarnessConfig(
        db_path=base / "harness.db",
        workspace_root=base / "workspace",
        log_path=base / "harness.log",
        default_project_name="demo",
        default_repo="accruvia/accruvia",
        runtime_backend="local",
        temporal_target="localhost:7233",
        temporal_namespace="default",
        temporal_task_queue="accruvia-harness",
        llm_backend="auto",
        llm_command=None,
        llm_codex_command=None,
        llm_claude_command=None,
        llm_accruvia_client_command=None,
    )


def _patched_build_engine(config_payload):
    if isinstance(config_payload, str):
        config = HarnessConfig.from_json(config_payload)
    else:
        config = HarnessConfig.from_payload(config_payload)
    store = SQLiteHarnessStore(config.db_path)
    store.initialize()
    engine = HarnessEngine(
        store=store,
        workspace_root=config.workspace_root,
        worker=LocalArtifactWorker(),
    )
    return engine


class TaskToStableActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.config = _minimal_config(self.base)
        self.store = SQLiteHarnessStore(self.config.db_path)
        self.store.initialize()
        self.project = Project(
            id=new_id("project"),
            name="test-project",
            description="Test project",
        )
        self.store.create_project(self.project)

    @patch("accruvia_harness.temporal_backend._build_engine", side_effect=_patched_build_engine)
    def test_task_to_stable_activity_runs_task_to_completion(self, _mock) -> None:
        task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            title="Stable activity task",
            objective="Run to completion via activity function",
            status=TaskStatus.PENDING,
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.store.create_task(task)

        result = task_to_stable_activity(self.config.to_json(), task.id)

        self.assertEqual(task.id, result["task_id"])
        self.assertIn(result["task_status"], ("completed", "failed"))
        self.assertGreaterEqual(result["run_count"], 1)

    @patch("accruvia_harness.temporal_backend._build_engine", side_effect=_patched_build_engine)
    def test_process_next_task_activity_processes_pending_task(self, _mock) -> None:
        task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            title="Queued activity task",
            objective="Be picked up by process_next_task_activity",
            status=TaskStatus.PENDING,
            priority=100,
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.store.create_task(task)

        result = process_next_task_activity(
            self.config.to_json(),
            project_id=self.project.id,
            worker_id="test-worker",
            lease_seconds=300,
        )

        self.assertIsNotNone(result)
        self.assertEqual(task.id, result["task_id"])
        self.assertIn(result["task_status"], ("completed", "failed"))
        self.assertGreaterEqual(result["run_count"], 1)

    def test_activity_propagates_engine_errors_for_missing_task(self) -> None:
        bogus_task_id = "task_does_not_exist"

        with self.assertRaises((ValueError, KeyError, TypeError)):
            task_to_stable_activity(self.config.to_json(), bogus_task_id)
