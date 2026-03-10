from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from accruvia_harness.domain import Project, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.runtime import LocalWorkflowRuntime, build_runtime
from accruvia_harness.store import SQLiteHarnessStore


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(base / "harness.db")
        self.store.initialize()
        self.engine = HarnessEngine(
            store=self.store,
            workspace_root=base / "workspace",
        )
        project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(project)
        self.project_id = project.id

    def test_local_runtime_runs_task_until_stable(self) -> None:
        runtime = LocalWorkflowRuntime(engine=self.engine)
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Runtime task",
            objective="Run through runtime boundary",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        result = runtime.run_task_until_stable(task.id)

        self.assertEqual("completed", result["task"].status.value)
        self.assertEqual(1, len(result["runs"]))

    def test_temporal_runtime_reports_unavailable_without_dependency(self) -> None:
        runtime = build_runtime(
            backend="temporal",
            engine=self.engine,
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="accruvia-harness",
        )

        with mock.patch("accruvia_harness.runtime.temporal_support_available", return_value=False):
            info = runtime.info()

        self.assertEqual("temporal", info.backend)
        self.assertIn("reason", info.details)

    def test_temporal_runtime_info_reports_available_when_supported(self) -> None:
        runtime = build_runtime(
            backend="temporal",
            engine=self.engine,
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="accruvia-harness",
        )

        with mock.patch("accruvia_harness.runtime.temporal_support_available", return_value=True):
            info = runtime.info()

        self.assertTrue(info.available)
        self.assertEqual("workflow_submission_ready", info.details["mode"])

    def test_temporal_runtime_normalizes_workflow_result_shape(self) -> None:
        runtime = build_runtime(
            backend="temporal",
            engine=self.engine,
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="accruvia-harness",
        )
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Temporal normalized task",
            objective="Normalize workflow result",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = self.engine.run_once(task.id)

        fake_client = mock.Mock()
        fake_client.execute_workflow = AsyncMock(return_value={"task_id": task.id, "task_status": "completed", "run_count": 1})

        fake_client_cls = mock.Mock()
        fake_client_cls.connect = AsyncMock(return_value=fake_client)

        with mock.patch("accruvia_harness.runtime._get_temporal_client_class", return_value=fake_client_cls):
            with mock.patch("accruvia_harness.runtime.temporal_support_available", return_value=True):
                result = runtime.run_task_until_stable(task.id)

        self.assertEqual(task.id, result["task"].id)
        self.assertEqual(run.id, result["runs"][0].id)
