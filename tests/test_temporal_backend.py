from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import Project, RunStatus, Task, TaskStatus, new_id
from accruvia_harness.store import SQLiteHarnessStore
from accruvia_harness.temporal_backend import create_run_activity


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


class CreateRunActivityTests(unittest.TestCase):
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
        self.task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            title="Test task",
            objective="A task for testing create_run_activity",
            status=TaskStatus.ACTIVE,
        )
        self.store.create_task(self.task)

    def test_creates_run_and_returns_id(self) -> None:
        result = create_run_activity(self.config.to_json(), self.task.id, attempt=1)
        self.assertIn("run_id", result)
        self.assertIn("workspace_root", result)
        self.assertTrue(result["run_id"].startswith("run_"))
        run = self.store.get_run(result["run_id"])
        self.assertIsNotNone(run)
        self.assertEqual(run.task_id, self.task.id)
        self.assertEqual(run.status, RunStatus.PLANNING)
        self.assertEqual(run.attempt, 1)

    def test_returns_configured_workspace_root(self) -> None:
        result = create_run_activity(self.config.to_json(), self.task.id, attempt=1)
        self.assertEqual(result["workspace_root"], str(self.base / "workspace"))

    def test_uses_caller_supplied_attempt(self) -> None:
        result = create_run_activity(self.config.to_json(), self.task.id, attempt=3)
        run = self.store.get_run(result["run_id"])
        self.assertEqual(run.attempt, 3)


if __name__ == "__main__":
    unittest.main()
