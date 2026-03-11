from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import EvaluationVerdict, Project, RunStatus, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.temporal_backend import (
    _build_engine,
    _process_next_timeout_seconds,
    _task_to_stable_timeout_seconds,
)
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
        self.config = HarnessConfig(
            db_path=base / "harness.db",
            workspace_root=base / "workspace",
            log_path=base / "harness.log",
            telemetry_dir=base / "telemetry",
            default_project_name="accruvia",
            default_repo="accruvia/accruvia",
            runtime_backend="local",
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="accruvia-harness",
            worker_backend="local",
            worker_command=None,
            llm_backend="auto",
            llm_model=None,
            llm_command=None,
            llm_codex_command=None,
            llm_claude_command=None,
            llm_accruvia_client_command=None,
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
            config=self.config,
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
            config=self.config,
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
            config=self.config,
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

    def test_temporal_engine_builder_uses_configured_external_modules(self) -> None:
        plugin_root = Path(self.temp_dir.name) / "plugins"
        plugin_root.mkdir()
        module_path = plugin_root / "temporal_private_adapter.py"
        module_path.write_text(
            "from pathlib import Path\n\n"
            "from accruvia_harness.adapters.base import AdapterEvidence\n\n"
            "class TemporalPrivateAdapter:\n"
            "    profile = 'temporal_private'\n\n"
            "    def build_evidence(self, task, run_dir: Path):\n"
            "        artifact = run_dir / 'temporal.txt'\n"
            "        artifact.write_text('temporal adapter output\\n', encoding='utf-8')\n"
            "        return AdapterEvidence(\n"
            "            passed=True,\n"
            "            report={\n"
            "                'changed_files': [str(artifact)],\n"
            "                'test_files': [],\n"
            "                'compile_check': {'passed': True, 'targets': [str(artifact)]},\n"
            "                'test_check': {'passed': True, 'framework': 'temporal-private'},\n"
            "            },\n"
            "            diagnostics={'adapter': 'temporal_private'},\n"
            "        )\n\n"
            "def register_adapters(registry):\n"
            "    registry.register(TemporalPrivateAdapter())\n",
            encoding="utf-8",
        )
        sys.path.insert(0, str(plugin_root))
        self.addCleanup(lambda: sys.path.remove(str(plugin_root)))

        config = HarnessConfig.from_payload(
            {
                **self.config.to_payload(),
                "adapter_modules": ("temporal_private_adapter",),
            }
        )
        engine = _build_engine(config.to_payload())
        project = engine.create_project("temporal-private", "Temporal private project")
        task = engine.create_task_with_policy(
            project_id=project.id,
            title="Temporal private task",
            objective="Use external adapter through temporal builder",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="temporal_private",
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)

        self.assertEqual(RunStatus.COMPLETED, run.status)

    def test_temporal_timeouts_are_derived_from_config(self) -> None:
        config = HarnessConfig.from_payload(
            {
                **self.config.to_payload(),
                "timeout_max_seconds": 1200,
            }
        )

        self.assertEqual(2460, _task_to_stable_timeout_seconds(config.to_payload()))
        self.assertEqual(1560, _process_next_timeout_seconds(config.to_payload(), 300))

    def test_blocked_run_uses_explicit_run_status_enum(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Blocked status task",
            objective="Surface blocked run status",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        report_dir = Path(self.temp_dir.name) / "workspace" / "runs"

        class BlockedWorker:
            def work(self, task, run, workspace_root):
                run_dir = workspace_root / "runs" / run.id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "plan.txt").write_text("plan\n", encoding="utf-8")
                (run_dir / "report.json").write_text('{"worker_outcome":"blocked"}', encoding="utf-8")
                from accruvia_harness.policy import WorkResult
                return WorkResult(
                    summary="blocked",
                    artifacts=[
                        ("plan", str(run_dir / "plan.txt"), "Plan"),
                        ("report", str(run_dir / "report.json"), "Report"),
                    ],
                    outcome="blocked",
                    diagnostics={"reason": "blocked"},
                )

        blocked_engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-blocked-runtime",
            worker=BlockedWorker(),
        )
        task = blocked_engine.create_task_with_policy(
            project_id=self.project_id,
            title="Blocked status task",
            objective="Surface blocked run status",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = blocked_engine.run_once(task.id)
        evaluation = self.store.list_evaluations(run.id)[0]

        self.assertEqual(RunStatus.BLOCKED, run.status)
        self.assertEqual(EvaluationVerdict.BLOCKED, evaluation.verdict)
