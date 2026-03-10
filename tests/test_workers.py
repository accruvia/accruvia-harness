from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import Run, RunStatus, Task, new_id
from accruvia_harness.llm import build_llm_router
from accruvia_harness.workers import (
    AgentCommandWorker,
    LocalArtifactWorker,
    LLMTaskWorker,
    ShellCommandWorker,
    build_worker,
    build_worker_from_config,
)


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.task = Task(
            id=new_id("task"),
            project_id="project_1",
            title="Worker task",
            objective="Verify worker abstraction",
        )
        self.run = Run(
            id=new_id("run"),
            task_id=self.task.id,
            status=RunStatus.WORKING,
            attempt=1,
            summary="",
        )

    def test_local_worker_creates_plan_and_report(self) -> None:
        worker = LocalArtifactWorker()
        result = worker.work(self.task, self.run, self.base)
        kinds = sorted(kind for kind, _, _ in result.artifacts)
        self.assertEqual(["plan", "report"], kinds)

    def test_shell_worker_executes_command(self) -> None:
        worker = ShellCommandWorker("printf 'hello from shell worker' > output.txt")
        result = worker.work(self.task, self.run, self.base)
        kinds = sorted(kind for kind, _, _ in result.artifacts)
        self.assertEqual(["report", "worker_stderr", "worker_stdout"], kinds)
        self.assertEqual("success", result.outcome)
        self.assertTrue((self.base / "runs" / self.run.id / "output.txt").exists())

    def test_agent_worker_captures_failure_without_raising(self) -> None:
        worker = AgentCommandWorker("printf 'boom' >&2; exit 7")
        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("failed", result.outcome)
        self.assertEqual(7, result.diagnostics["returncode"])
        self.assertTrue((self.base / "runs" / self.run.id / "worker.stderr.txt").exists())

    def test_build_worker_requires_command_for_shell_backend(self) -> None:
        with self.assertRaises(ValueError):
            build_worker("shell")

    def test_build_worker_requires_command_for_agent_backend(self) -> None:
        with self.assertRaises(ValueError):
            build_worker("agent")

    def test_llm_worker_executes_routed_codex_command(self) -> None:
        config = HarnessConfig(
            db_path=self.base / "harness.db",
            workspace_root=self.base,
            log_path=self.base / "harness.log",
            default_project_name="demo",
            default_repo="accruvia/accruvia",
            runtime_backend="local",
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="accruvia-harness",
            worker_backend="llm",
            worker_command=None,
            llm_backend="codex",
            llm_model="gpt-5.4-codex",
            llm_command=None,
            llm_codex_command="printf 'codex response' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"",
            llm_claude_command=None,
            llm_accruvia_client_command=None,
        )

        worker = build_worker_from_config(config)
        self.assertIsInstance(worker, LLMTaskWorker)
        result = worker.work(self.task, self.run, self.base)

        kinds = sorted(kind for kind, _, _ in result.artifacts)
        self.assertEqual(["llm_response", "plan", "report"], kinds)
        self.assertEqual("success", result.outcome)
        self.assertEqual("codex", result.diagnostics["llm_backend"])

    def test_llm_router_prefers_accruvia_client_in_github_actions(self) -> None:
        config = HarnessConfig(
            db_path=self.base / "harness.db",
            workspace_root=self.base,
            log_path=self.base / "harness.log",
            default_project_name="demo",
            default_repo="accruvia/accruvia",
            runtime_backend="local",
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="accruvia-harness",
            worker_backend="llm",
            worker_command=None,
            llm_backend="auto",
            llm_model=None,
            llm_command=None,
            llm_codex_command="printf 'codex response' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"",
            llm_claude_command=None,
            llm_accruvia_client_command="printf 'accruvia response' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"",
        )

        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=False):
            executor, backend = build_llm_router(config).resolve()

        self.assertEqual("accruvia_client", backend)
        self.assertEqual("accruvia_client", executor.backend_name)
