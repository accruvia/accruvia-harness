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
        self.fixtures = self.repo_root / "tests" / "fixtures"
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

    def test_ops_report_includes_validation_profile_metrics(self) -> None:
        project = self.run_cli("create-project", "ops", "ops project")["project"]
        task = self.run_cli(
            "create-task",
            project["id"],
            "Task Ops",
            "Objective Ops",
            "--validation-profile",
            "python",
        )["task"]
        self.run_cli("run-once", task["id"])
        self.run_cli("review-promotion", task["id"])

        ops = self.run_cli("ops-report", "--project-id", project["id"])

        self.assertEqual(1, ops["metrics"]["pending_promotions"])
        self.assertEqual(1, ops["metrics"]["tasks_by_validation_profile"]["python"])
        self.assertGreaterEqual(ops["telemetry"]["metric_totals"]["run_started"], 1)

    def test_telemetry_report_is_emitted_after_run(self) -> None:
        project = self.run_cli("create-project", "telemetry", "telemetry project")["project"]
        task = self.run_cli(
            "create-task",
            project["id"],
            "Task Telemetry",
            "Objective Telemetry",
        )["task"]

        self.run_cli("run-once", task["id"])
        telemetry = self.run_cli("telemetry-report")

        self.assertGreaterEqual(telemetry["metric_totals"]["run_started"], 1)
        self.assertGreaterEqual(telemetry["metric_totals"]["run_finished"], 1)
        self.assertIn("planning", telemetry["span_counts"])

    def test_javascript_profile_runs_end_to_end_through_review(self) -> None:
        project = self.run_cli("create-project", "js", "javascript project")["project"]
        task = self.run_cli(
            "create-task",
            project["id"],
            "Task JS",
            "Objective JS",
            "--validation-profile",
            "javascript",
        )["task"]

        self.run_cli("run-once", task["id"])
        review = self.run_cli("review-promotion", task["id"])
        report = self.run_cli("task-report", task["id"])

        self.assertEqual("pending", review["promotion"]["status"])
        artifact_report = next(item for item in report["runs"][0]["artifacts"] if item["kind"] == "report")
        payload = json.loads(Path(artifact_report["path"]).read_text(encoding="utf-8"))
        self.assertEqual("javascript", payload["validation_profile"])
        self.assertEqual("node_test", payload["test_check"]["framework"])

    def test_supervise_drains_queue_until_idle(self) -> None:
        project = self.run_cli("create-project", "supervisor", "supervisor project")["project"]
        high = self.run_cli(
            "create-task",
            project["id"],
            "High",
            "High priority",
            "--priority",
            "300",
        )["task"]
        low = self.run_cli(
            "create-task",
            project["id"],
            "Low",
            "Low priority",
            "--priority",
            "100",
        )["task"]

        result = self.run_cli("supervise", "--project-id", project["id"], "--worker-id", "supervisor-a")
        summary = self.run_cli("summary", "--project-id", project["id"])

        self.assertEqual(2, result["processed_count"])
        self.assertEqual([high["id"], low["id"]], result["processed_task_ids"])
        self.assertEqual("idle", result["exit_reason"])
        self.assertEqual(2, summary["metrics"]["tasks_by_status"]["completed"])

    def test_terraform_profile_runs_end_to_end_through_review(self) -> None:
        project = self.run_cli("create-project", "tf", "terraform project")["project"]
        task = self.run_cli(
            "create-task",
            project["id"],
            "Task TF",
            "Objective TF",
            "--validation-profile",
            "terraform",
        )["task"]

        self.run_cli("run-once", task["id"])
        review = self.run_cli("review-promotion", task["id"])
        report = self.run_cli("task-report", task["id"])

        self.assertEqual("pending", review["promotion"]["status"])
        artifact_report = next(item for item in report["runs"][0]["artifacts"] if item["kind"] == "report")
        payload = json.loads(Path(artifact_report["path"]).read_text(encoding="utf-8"))
        self.assertEqual("terraform", payload["validation_profile"])
        self.assertIn("terraform_validate", payload)
        self.assertTrue(payload["terraform_validate"]["passed"])

    def test_review_then_affirm_promotion_commands_record_final_approval(self) -> None:
        project = self.run_cli("create-project", "promotion", "promotion project")["project"]
        task = self.run_cli(
            "create-task",
            project["id"],
            "Task B",
            "Objective B",
        )["task"]
        self.run_cli("run-once", task["id"])
        self.env["ACCRUVIA_LLM_BACKEND"] = "command"
        self.env["ACCRUVIA_LLM_COMMAND"] = f'bash "{self.fixtures / "fake_affirm_approve.sh"}"'

        review = self.run_cli("review-promotion", task["id"])
        affirmation = self.run_cli("affirm-promotion", task["id"])
        status = self.run_cli("status")

        self.assertEqual("pending", review["promotion"]["status"])
        self.assertEqual("approved", affirmation["promotion"]["status"])
        self.assertEqual(1, len(status["promotions"]))

    def test_explain_system_and_task_use_read_only_llm_observer(self) -> None:
        project = self.run_cli("create-project", "observer", "observer project")["project"]
        task = self.run_cli(
            "create-task",
            project["id"],
            "Task Observer",
            "Objective Observer",
        )["task"]
        self.run_cli("run-once", task["id"])
        self.env["ACCRUVIA_LLM_BACKEND"] = "command"
        self.env["ACCRUVIA_LLM_COMMAND"] = f'bash "{self.fixtures / "fake_affirm_approve.sh"}"'

        system_explanation = self.run_cli("explain-system", "--project-id", project["id"])
        task_explanation = self.run_cli("explain-task", task["id"])
        status = self.run_cli("status")

        self.assertIn("APPROVE", system_explanation["explanation"])
        self.assertIn("APPROVE", task_explanation["explanation"])
        self.assertEqual(1, len(status["tasks"]))
        self.assertEqual(1, len(status["runs"]))
