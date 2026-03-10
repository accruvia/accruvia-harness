from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_root = Path(__file__).resolve().parents[1]
        self.env = os.environ.copy()
        self.env["ACCRUVIA_HARNESS_HOME"] = self.temp_dir.name
        self.env["PYTHONPATH"] = str(self.repo_root / "src")

    def run_cli(self, *args: str) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, "-m", "accruvia_harness", *args],
            cwd=self.repo_root,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def test_summary_context_packet_and_task_report(self) -> None:
        project = self.run_cli("create-project", "demo", "demo project")["project"]
        task = self.run_cli(
            "create-task",
            project["id"],
            "Task A",
            "Objective A",
            "--priority",
            "200",
        )["task"]

        self.run_cli("process-next", "--worker-id", "cli-worker", "--lease-seconds", "60")
        summary = self.run_cli("summary", "--project-id", project["id"])
        context_packet = self.run_cli("context-packet", "--project-id", project["id"])
        task_report = self.run_cli("task-report", task["id"])

        self.assertEqual(project["id"], summary["project_id"])
        self.assertEqual(1, summary["metrics"]["tasks_by_status"]["completed"])
        self.assertEqual(project["id"], context_packet["project_id"])
        self.assertEqual(task["id"], task_report["task"]["id"])
        self.assertEqual(1, len(task_report["runs"]))

    def test_review_promotion_command_records_approval(self) -> None:
        project = self.run_cli("create-project", "promotion", "promotion project")["project"]
        task = self.run_cli(
            "create-task",
            project["id"],
            "Task B",
            "Objective B",
        )["task"]
        self.run_cli("run-once", task["id"])

        review = self.run_cli("review-promotion", task["id"])
        status = self.run_cli("status")

        self.assertEqual("approved", review["promotion"]["status"])
        self.assertEqual(1, len(status["promotions"]))
