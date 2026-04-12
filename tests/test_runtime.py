from __future__ import annotations

import asyncio
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
    connect_temporal_client,
    _next_task_runtime_budget_seconds,
    _task_runtime_budget_seconds,
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
            llm_backend="auto",

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

        fake_workflow_service = mock.Mock()
        fake_workflow_service.describe_namespace = AsyncMock(return_value=object())
        fake_service_client = mock.Mock()
        fake_service_client.workflow_service = fake_workflow_service
        fake_client = mock.Mock()
        fake_client.service_client = fake_service_client
        fake_client.execute_workflow = AsyncMock(return_value={"task_id": task.id, "task_status": "completed", "run_count": 1})

        fake_client_cls = mock.Mock()
        fake_client_cls.connect = AsyncMock(return_value=fake_client)

        with mock.patch("accruvia_harness.runtime._get_temporal_client_class", return_value=fake_client_cls):
            with mock.patch("accruvia_harness.runtime.temporal_support_available", return_value=True):
                result = runtime.run_task_until_stable(task.id)

        self.assertEqual(task.id, result["task"].id)
        self.assertEqual(run.id, result["runs"][0].id)

    def test_connect_temporal_client_retries_until_temporal_is_ready(self) -> None:
        fake_workflow_service = mock.Mock()
        fake_workflow_service.describe_namespace = AsyncMock(return_value=object())
        fake_service_client = mock.Mock()
        fake_service_client.workflow_service = fake_workflow_service
        fake_client = mock.Mock()
        fake_client.service_client = fake_service_client
        client_cls = mock.Mock()
        client_cls.connect = AsyncMock(
            side_effect=[
                RuntimeError("Connection refused"),
                RuntimeError("Connection refused"),
                fake_client,
            ]
        )

        result = asyncio.run(
            connect_temporal_client(
                client_cls,
                "localhost:7233",
                "default",
                attempts=3,
                delay_seconds=0,
            )
        )

        self.assertIs(result, fake_client)
        self.assertEqual(3, client_cls.connect.await_count)
        # describe_namespace is only called when temporalio is installed
        # (DescribeNamespaceRequest is not None); skip assertion otherwise.
        from accruvia_harness.temporal_backend import DescribeNamespaceRequest
        if DescribeNamespaceRequest is not None:
            fake_workflow_service.describe_namespace.assert_awaited_once()

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
        # Skills is the only production worker backend; this test exercises
        # the adapter-module loading path through the temporal builder, not
        # the skills pipeline, so swap in the no-LLM LocalArtifactWorker.
        from accruvia_harness.workers import LocalArtifactWorker
        from accruvia_harness.adapters import build_adapter_registry
        engine.set_worker(LocalArtifactWorker(
            adapter_registry=build_adapter_registry(config.adapter_modules),
        ))
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

        self.assertEqual(1260, _task_to_stable_timeout_seconds(config.to_payload()))
        self.assertEqual(1560, _process_next_timeout_seconds(config.to_payload(), 300))

    def test_temporal_task_budget_uses_task_retry_and_branch_policy(self) -> None:
        config = HarnessConfig.from_payload(
            {
                **self.config.to_payload(),
                "timeout_max_seconds": 600,
                "db_path": str(self.store.db_path),
            }
        )
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Budgeted task",
            objective="Budget actual policy",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=4,
            max_branches=3,
            required_artifacts=["plan", "report"],
        )

        self.assertEqual(4440, _task_runtime_budget_seconds(config.to_payload(), task.id))
        self.assertEqual(4740, _next_task_runtime_budget_seconds(config.to_payload(), self.project_id, 300))

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

    def test_config_reads_dedicated_heartbeat_timeout(self) -> None:
        with mock.patch.dict("os.environ", {"ACCRUVIA_HEARTBEAT_TIMEOUT_SECONDS": "2400"}, clear=False):
            config = HarnessConfig.from_env(
                db_path=self.config.db_path,
                workspace_root=self.config.workspace_root,
                log_path=self.config.log_path,
            )

        self.assertEqual(2400, config.heartbeat_timeout_seconds)

    def test_scope_failure_early_exit(self) -> None:
        from accruvia_harness.policy import WorkResult

        class ScopeFailWorker:
            def work(self, task, run, workspace_root, retry_hints=None):
                run_dir = workspace_root / "runs" / run.id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "plan.txt").write_text("plan\n", encoding="utf-8")
                (run_dir / "report.json").write_text(
                    '{"worker_outcome":"failed","failure_category":"scope_skill_failure"}',
                    encoding="utf-8",
                )
                return WorkResult(
                    summary="Scope skill failed to produce valid output.",
                    artifacts=[
                        ("plan", str(run_dir / "plan.txt"), "Plan"),
                        ("report", str(run_dir / "report.json"), "Report"),
                    ],
                    outcome="failed",
                    diagnostics={
                        "stage": "scope",
                        "failure_category": "scope_skill_failure",
                    },
                )

        scope_fail_engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-scope-fail",
            worker=ScopeFailWorker(),
        )
        task = scope_fail_engine.create_task_with_policy(
            project_id=self.project_id,
            title="Scope fail task",
            objective="Trigger early exit on scope failure",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = scope_fail_engine.run_once(task.id)

        self.assertEqual(RunStatus.FAILED, run.status)

    def test_diagnose_triggered(self) -> None:
        from accruvia_harness.policy import WorkResult

        class DiagnoseWorker:
            def work(self, task, run, workspace_root, retry_hints=None):
                run_dir = workspace_root / "runs" / run.id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "plan.txt").write_text("plan\n", encoding="utf-8")
                (run_dir / "report.json").write_text(
                    '{"worker_outcome":"failed","failure_category":"code_defect","diagnostics":{"classification":"code_defect"}}',
                    encoding="utf-8",
                )
                return WorkResult(
                    summary="Validation failed — diagnosed as code defect.",
                    artifacts=[
                        ("plan", str(run_dir / "plan.txt"), "Plan"),
                        ("report", str(run_dir / "report.json"), "Report"),
                    ],
                    outcome="failed",
                    diagnostics={
                        "stage": "validate",
                        "failure_category": "code_defect",
                        "classification": "code_defect",
                    },
                )

        diag_engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-diagnose",
            worker=DiagnoseWorker(),
        )
        task = diag_engine.create_task_with_policy(
            project_id=self.project_id,
            title="Diagnose trigger task",
            objective="Trigger diagnose stage on validation failure",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = diag_engine.run_once(task.id)

        self.assertEqual(RunStatus.FAILED, run.status)
        evals = self.store.list_evaluations(run.id)
        self.assertGreaterEqual(len(evals), 1)
        self.assertEqual(EvaluationVerdict.FAILED, evals[0].verdict)

    def test_fix_tests_loop_bounded(self) -> None:
        from accruvia_harness.services.work_orchestrator import SkillsWorkOrchestrator

        max_rounds_attr = None
        import accruvia_harness.services.work_orchestrator as wo_mod
        source = open(wo_mod.__file__, "r").read()
        import re
        match = re.search(r"_MAX_FIX_ROUNDS\s*=\s*(\d+)", source)
        self.assertIsNotNone(match, "_MAX_FIX_ROUNDS constant not found in work_orchestrator")
        max_rounds = int(match.group(1))
        self.assertGreaterEqual(max_rounds, 1)
        self.assertLessEqual(max_rounds, 5)

    def test_routes_skills_backend(self) -> None:
        from accruvia_harness.workers import build_worker_from_config
        from accruvia_harness.skills_worker import SkillsWorker

        config = HarnessConfig.from_payload(
            {
                **self.config.to_payload(),
                "llm_backend": "command",
                "llm_command": "echo '{}'",
            }
        )

        worker = build_worker_from_config(config)

        self.assertIsInstance(worker, SkillsWorker)
