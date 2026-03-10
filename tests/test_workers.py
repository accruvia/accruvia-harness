from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import Run, RunStatus, Task, new_id
from accruvia_harness.workers import LocalArtifactWorker, ShellCommandWorker, build_worker


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
        self.assertTrue((self.base / "runs" / self.run.id / "output.txt").exists())

    def test_build_worker_requires_command_for_shell_backend(self) -> None:
        with self.assertRaises(ValueError):
            build_worker("shell")
