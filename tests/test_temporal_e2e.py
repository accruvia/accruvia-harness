from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


def _temporal_available() -> bool:
    try:
        import temporalio  # noqa: F401
    except ModuleNotFoundError:
        return False
    try:
        with socket.create_connection(("127.0.0.1", 7233), timeout=1):
            return True
    except OSError:
        return False


@unittest.skipUnless(_temporal_available(), "Temporal dev stack is not reachable on localhost:7233")
class TemporalEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_root = Path(__file__).resolve().parents[1]
        self.env = os.environ.copy()
        self.env["ACCRUVIA_HARNESS_HOME"] = self.temp_dir.name
        self.env["PYTHONPATH"] = str(self.repo_root / "src")
        self.env["ACCRUVIA_HARNESS_RUNTIME"] = "temporal"

        self.worker = subprocess.Popen(
            [sys.executable, "-m", "accruvia_harness", "run-temporal-worker"],
            cwd=self.repo_root,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._cleanup_worker)
        time.sleep(2)

    def _cleanup_worker(self) -> None:
        if self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait(timeout=5)

    def run_cli(self, *args: str) -> str:
        retries = 10 if args and args[0] == "run-runtime" else 1
        last_error: subprocess.CalledProcessError | None = None
        for attempt in range(retries):
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "accruvia_harness", "--json", *args],
                    cwd=self.repo_root,
                    env=self.env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return completed.stdout
            except subprocess.CalledProcessError as exc:
                last_error = exc
                if attempt == retries - 1:
                    raise
                time.sleep(1)
        assert last_error is not None
        raise last_error

    def test_temporal_runtime_completes_task_end_to_end(self) -> None:
        import json

        project = json.loads(
            self.run_cli(
                "create-project",
                "temporal-e2e",
                "Temporal E2E",
                "--no-bootstrap-heartbeat",
            )
        )["project"]
        task = json.loads(
            self.run_cli("create-task", project["id"], "Temporal task", "Run through Temporal")
        )["task"]

        result = json.loads(self.run_cli("run-runtime", task["id"]))

        self.assertEqual("completed", result["task"]["status"])
        self.assertGreaterEqual(len(result["runs"]), 1)
        self.assertEqual("completed", result["runs"][-1]["status"])


class _MockSkillsWorker:
    """Deterministic worker backend that simulates a successful skills pipeline."""

    def set_progress_callback(self, callback):
        pass

    def work(self, task, run, workspace_root, retry_hints=None):
        from accruvia_harness.policy import WorkResult

        run_dir = Path(workspace_root) / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)

        plan_path = run_dir / "plan.json"
        plan_path.write_text(
            json.dumps({"task_id": task.id, "approach": "mock_skills", "files_to_touch": []}),
            encoding="utf-8",
        )

        report_path = run_dir / "report.json"
        report_path.write_text(
            json.dumps({"task_id": task.id, "run_id": run.id, "outcome": "success"}),
            encoding="utf-8",
        )

        return WorkResult(
            summary="Mock skills pipeline completed",
            artifacts=[
                ("plan", str(plan_path), "Mock plan"),
                ("report", str(report_path), "Mock report"),
            ],
            outcome="success",
            diagnostics={"worker_backend": "mock_skills", "stage": "complete"},
        )


@unittest.skipUnless(_temporal_available(), "Temporal dev stack is not reachable on localhost:7233")
class TemporalSkillsPipelineTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)

        from accruvia_harness.config import HarnessConfig
        from accruvia_harness.bootstrap import build_engine_from_config

        self.task_queue = f"skills-e2e-{uuid4().hex[:8]}"
        self.config = HarnessConfig(
            db_path=base / "harness.db",
            workspace_root=base / "workspace",
            log_path=base / "harness.log",
            default_project_name="skills-e2e",
            default_repo="test/skills-e2e",
            runtime_backend="temporal",
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue=self.task_queue,
            llm_backend="command",
            llm_model=None,
            llm_command="echo '{}'",
            llm_codex_command=None,
            llm_claude_command=None,
            llm_accruvia_client_command=None,
            timeout_max_seconds=60,
        )

        self.engine = build_engine_from_config(self.config)
        self.engine.set_worker(_MockSkillsWorker())

        def _patched_build(config_payload):
            from accruvia_harness.config import HarnessConfig as _HC
            from accruvia_harness.bootstrap import build_engine_from_config as _build
            if isinstance(config_payload, str):
                cfg = _HC.from_json(config_payload)
            else:
                cfg = _HC.from_payload(config_payload)
            eng = _build(cfg)
            eng.set_worker(_MockSkillsWorker())
            return eng

        self._patcher = patch(
            "accruvia_harness.temporal_backend._build_engine",
            _patched_build,
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

        self._worker_loop = asyncio.new_event_loop()
        self._worker_thread = threading.Thread(
            target=self._run_worker_loop,
            daemon=True,
        )
        self._worker_thread.start()
        self.addCleanup(self._stop_worker)
        time.sleep(2)

    def _run_worker_loop(self) -> None:
        asyncio.set_event_loop(self._worker_loop)
        self._worker_loop.run_until_complete(self._start_temporal_worker())

    async def _start_temporal_worker(self) -> None:
        from temporalio.client import Client
        from temporalio.worker import Worker
        from accruvia_harness.temporal_backend import (
            build_temporal_workflows,
            connect_temporal_client,
            task_to_stable_activity_defn,
            create_run_activity_defn,
            process_next_task_activity_defn,
        )

        client = await connect_temporal_client(
            Client,
            self.config.temporal_target,
            self.config.temporal_namespace,
        )
        workflows = build_temporal_workflows()
        worker = Worker(
            client,
            task_queue=self.task_queue,
            workflows=workflows,
            activities=[
                task_to_stable_activity_defn,
                create_run_activity_defn,
                process_next_task_activity_defn,
            ],
        )
        await worker.run()

    def _stop_worker(self) -> None:
        if self._worker_loop.is_running():
            self._worker_loop.call_soon_threadsafe(self._worker_loop.stop)
        self._worker_thread.join(timeout=5)

    def test_full_skills_pipeline_completes(self) -> None:
        project = self.engine.create_project(
            name="Skills E2E Project",
            description="E2E test for skills pipeline via Temporal",
        )
        task = self.engine.create_task(
            project_id=project.id,
            title="Skills pipeline test",
            objective="Verify the skills pipeline completes through Temporal",
        )

        from accruvia_harness.runtime import TemporalWorkflowRuntime

        runtime = TemporalWorkflowRuntime(
            config=self.config,
            engine=self.engine,
            target=self.config.temporal_target,
            namespace=self.config.temporal_namespace,
            task_queue=self.task_queue,
        )

        result = runtime.run_task_until_stable(task.id)

        final_task = result["task"]
        self.assertIn(final_task.status.value, ("completed", "stable"))

        runs = result["runs"]
        self.assertGreaterEqual(len(runs), 1)

        last_run = runs[-1]
        self.assertIn(
            last_run.status.value,
            {"completed", "failed", "disposed"},
        )

        statuses_seen = {r.status.value for r in runs}
        self.assertTrue(
            statuses_seen & {"completed", "failed"},
            f"Expected at least one terminal run, got statuses: {statuses_seen}",
        )
