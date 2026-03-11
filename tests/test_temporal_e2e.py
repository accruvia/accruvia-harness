from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


def _temporal_available() -> bool:
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
                    [sys.executable, "-m", "accruvia_harness", *args],
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

        project = json.loads(self.run_cli("create-project", "temporal-e2e", "Temporal E2E"))["project"]
        task = json.loads(
            self.run_cli("create-task", project["id"], "Temporal task", "Run through Temporal")
        )["task"]

        result = json.loads(self.run_cli("run-runtime", task["id"]))

        self.assertEqual("completed", result["task"]["status"])
        self.assertGreaterEqual(len(result["runs"]), 1)
        self.assertEqual("completed", result["runs"][-1]["status"])
