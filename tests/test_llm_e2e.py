from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LLMEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_root = Path(__file__).resolve().parents[1]
        self.fixtures = self.repo_root / "tests" / "fixtures"
        self.env = os.environ.copy()
        self.env["ACCRUVIA_HARNESS_HOME"] = self.temp_dir.name
        self.env["PYTHONPATH"] = str(self.repo_root / "src")
        self.env["ACCRUVIA_WORKER_BACKEND"] = "llm"
        self.env["ACCRUVIA_LLM_BACKEND"] = "auto"
        self.env["ACCRUVIA_LLM_MODEL"] = "openai-codex/gpt-5.4"

    def run_cli(self, *args: str) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, "-m", "accruvia_harness", "--json", *args],
            cwd=self.repo_root,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def create_task(self, max_attempts: int = 3) -> tuple[dict[str, object], dict[str, object]]:
        project = self.run_cli("create-project", "llm-e2e", "LLM E2E")["project"]
        task = self.run_cli(
            "create-task",
            project["id"],
            "LLM task",
            "Verify routed LLM execution end to end",
            "--max-attempts",
            str(max_attempts),
            "--required-artifact",
            "plan",
            "--required-artifact",
            "report",
        )["task"]
        return project, task

    def test_auto_route_prefers_codex_locally(self) -> None:
        self.env["ACCRUVIA_LLM_CODEX_COMMAND"] = f'bash "{self.fixtures / "fake_codex.sh"}"'

        _, task = self.create_task()
        processed = self.run_cli("process-next", "--worker-id", "llm-worker", "--lease-seconds", "60")
        report = self.run_cli("task-report", task["id"])

        self.assertEqual(task["id"], processed["task"]["id"])
        self.assertEqual("completed", report["task"]["status"])
        run_report = report["runs"][0]
        artifact_kinds = sorted(item["kind"] for item in run_report["artifacts"])
        self.assertEqual(["llm_response", "plan", "report", "workspace_metadata"], artifact_kinds)
        response_artifact = next(item for item in run_report["artifacts"] if item["kind"] == "llm_response")
        report_artifact = next(item for item in run_report["artifacts"] if item["kind"] == "report")
        response_text = Path(response_artifact["path"]).read_text(encoding="utf-8")
        report_payload = json.loads(Path(report_artifact["path"]).read_text(encoding="utf-8"))
        self.assertIn("executor=codex", response_text)
        self.assertEqual("codex", report_payload["llm_backend"])

    def test_auto_route_prefers_accruvia_client_in_github_actions(self) -> None:
        self.env["GITHUB_ACTIONS"] = "true"
        self.env["ACCRUVIA_LLM_CODEX_COMMAND"] = f'bash "{self.fixtures / "fake_codex.sh"}"'
        self.env["ACCRUVIA_LLM_ACCRUVIA_CLIENT_COMMAND"] = (
            f'bash "{self.fixtures / "fake_accruvia_client.sh"}"'
        )

        _, task = self.create_task()
        self.run_cli("process-next", "--worker-id", "ci-llm-worker", "--lease-seconds", "60")
        report = self.run_cli("task-report", task["id"])

        run_report = report["runs"][0]
        response_artifact = next(item for item in run_report["artifacts"] if item["kind"] == "llm_response")
        report_artifact = next(item for item in run_report["artifacts"] if item["kind"] == "report")
        response_text = Path(response_artifact["path"]).read_text(encoding="utf-8")
        report_payload = json.loads(Path(report_artifact["path"]).read_text(encoding="utf-8"))
        self.assertIn("executor=accruvia_client", response_text)
        self.assertEqual("accruvia_client", report_payload["llm_backend"])

    def test_failed_executor_records_precise_failure(self) -> None:
        self.env["ACCRUVIA_LLM_COMMAND"] = f'bash "{self.fixtures / "fake_llm_fail.sh"}"'

        _, task = self.create_task(max_attempts=1)
        processed = self.run_cli("process-next", "--worker-id", "failing-llm-worker", "--lease-seconds", "60")
        report = self.run_cli("task-report", task["id"])

        self.assertEqual(task["id"], processed["task"]["id"])
        self.assertEqual("failed", report["task"]["status"])
        run_report = report["runs"][-1]
        artifact_kinds = sorted(item["kind"] for item in run_report["artifacts"])
        self.assertEqual(["llm_error", "report", "workspace_metadata"], artifact_kinds)
        self.assertEqual("failed", run_report["run"]["status"])
        self.assertEqual("failed", run_report["evaluations"][0]["verdict"])
