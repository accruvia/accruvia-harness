from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accruvia_harness.atomicity import atomicity_gate
from accruvia_harness.agent_worker import _focused_test_command, run_agent_worker, select_worker_llm_command
from accruvia_harness.adapters import build_adapter_registry
from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import Run, RunStatus, Task, new_id
from accruvia_harness.llm import CommandLLMExecutor, LLMInvocation, build_llm_router, parse_affirmation_response
from accruvia_harness.resource_limits import resolve_memory_limit_mb
from accruvia_harness.subprocess_env import build_subprocess_env
from accruvia_harness.telemetry import TelemetrySink
from accruvia_harness.timeout_policy import ExecutionTimeoutPolicy
from accruvia_harness.workers import (
    AgentCommandWorker,
    LocalArtifactWorker,
    LLMTaskWorker,
    ShellCommandWorker,
    _default_agent_worker_command,
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

    def test_local_worker_emits_javascript_profile_evidence(self) -> None:
        self.task.validation_profile = "javascript"

        worker = LocalArtifactWorker()
        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("success", result.outcome)
        report_path = self.base / "runs" / self.run.id / "report.json"
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual("javascript", payload["validation_profile"])
        self.assertTrue(payload["test_files"][0].endswith(".test.js"))
        self.assertEqual("node_test", payload["test_check"]["framework"])

    def test_local_worker_emits_terraform_profile_evidence(self) -> None:
        self.task.validation_profile = "terraform"

        worker = LocalArtifactWorker()
        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("success", result.outcome)
        report_path = self.base / "runs" / self.run.id / "report.json"
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual("terraform", payload["validation_profile"])
        self.assertIn("terraform_validate", payload)
        self.assertTrue(payload["terraform_validate"]["passed"])

    def test_local_worker_can_load_external_adapter_module(self) -> None:
        plugin_root = self.base / "plugins"
        plugin_root.mkdir()
        module_path = plugin_root / "private_adapter.py"
        module_path.write_text(
            "from pathlib import Path\n\n"
            "from accruvia_harness.adapters.base import AdapterEvidence\n\n"
            "class PrivateAdapter:\n"
            "    profile = 'private_profile'\n\n"
            "    def build_evidence(self, task, run_dir: Path):\n"
            "        artifact = run_dir / 'private.txt'\n"
            "        artifact.write_text('private adapter output\\n', encoding='utf-8')\n"
            "        return AdapterEvidence(\n"
            "            passed=True,\n"
            "            report={\n"
            "                'changed_files': [str(artifact)],\n"
            "                'test_files': [],\n"
            "                'compile_check': {'passed': True, 'targets': [str(artifact)]},\n"
            "                'test_check': {'passed': True, 'framework': 'private'},\n"
            "            },\n"
            "            diagnostics={'adapter': 'private_profile'},\n"
            "        )\n\n"
            "def register_adapters(registry):\n"
            "    registry.register(PrivateAdapter())\n",
            encoding="utf-8",
        )
        sys.path.insert(0, str(plugin_root))
        self.addCleanup(lambda: sys.path.remove(str(plugin_root)))

        self.task.validation_profile = "private_profile"
        worker = LocalArtifactWorker(
            adapter_registry=build_adapter_registry(("private_adapter",))
        )
        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("success", result.outcome)
        report_path = self.base / "runs" / self.run.id / "report.json"
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual("private_profile", payload["validation_profile"])
        self.assertEqual("private_profile", result.diagnostics["adapter"])

    def test_shell_worker_executes_command(self) -> None:
        worker = ShellCommandWorker("printf 'hello from shell worker' > output.txt")
        result = worker.work(self.task, self.run, self.base)
        kinds = sorted(kind for kind, _, _ in result.artifacts)
        self.assertEqual(["report", "worker_stderr", "worker_stdout"], kinds)
        self.assertEqual("success", result.outcome)
        self.assertTrue((self.base / "runs" / self.run.id / "output.txt").exists())

    def test_shell_worker_times_out_with_policy(self) -> None:
        telemetry = TelemetrySink(self.base / "telemetry")
        timeout_policy = ExecutionTimeoutPolicy(
            telemetry,
            min_seconds=1,
            max_seconds=1,
            multiplier=1.0,
        )
        worker = ShellCommandWorker("sleep 2", timeout_policy=timeout_policy)

        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("blocked", result.outcome)
        self.assertTrue(result.diagnostics["timed_out"])
        self.assertEqual(1, result.diagnostics["timeout_seconds"])

    def test_shell_worker_timeout_handles_bytes_stderr(self) -> None:
        telemetry = TelemetrySink(self.base / "telemetry")
        timeout_policy = ExecutionTimeoutPolicy(
            telemetry,
            min_seconds=1,
            max_seconds=1,
            multiplier=1.0,
        )
        monotonic_values = iter([0.0, 2.0])

        class _FakeProcess:
            pid = 12345
            returncode = -9

            def poll(self):
                return None

            def kill(self):
                return None

            def communicate(self):
                return (b"partial stdout", b"partial stderr")

        worker = ShellCommandWorker(
            "sleep 2",
            timeout_policy=timeout_policy,
            monotonic=lambda: next(monotonic_values),
            sleep_fn=lambda _seconds: None,
        )

        with patch("accruvia_harness.workers.subprocess.Popen", return_value=_FakeProcess()):
            result = worker.work(self.task, self.run, self.base)

        stderr_path = self.base / "runs" / self.run.id / "worker.stderr.txt"
        self.assertEqual("blocked", result.outcome)
        self.assertEqual("partial stderr", stderr_path.read_text(encoding="utf-8"))

    def test_agent_worker_captures_failure_without_raising(self) -> None:
        worker = AgentCommandWorker("printf 'boom' >&2; exit 7")
        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("blocked", result.outcome)
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
            llm_codex_command="printf 'codex response' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"; printf '{\"cost_usd\": 0.12, \"prompt_tokens\": 10, \"completion_tokens\": 20, \"total_tokens\": 30, \"latency_ms\": 250, \"model\": \"gpt-5.4-codex\"}' > \"$ACCRUVIA_LLM_METADATA_PATH\"",
            llm_claude_command=None,
            llm_accruvia_client_command=None,
        )

        telemetry = TelemetrySink(self.base / "telemetry")
        worker = build_worker_from_config(config, telemetry=telemetry)
        self.assertIsInstance(worker, LLMTaskWorker)
        result = worker.work(self.task, self.run, self.base)

        kinds = sorted(kind for kind, _, _ in result.artifacts)
        self.assertEqual(["llm_response", "plan", "report"], kinds)
        self.assertEqual("success", result.outcome)
        self.assertEqual("codex", result.diagnostics["llm_backend"])
        self.assertEqual(0.12, result.diagnostics["cost_usd"])
        summary = telemetry.summary()
        self.assertEqual(0.12, summary["cost_totals"]["cost_usd"])
        self.assertEqual(30.0, summary["cost_totals"]["total_tokens"])

    def test_command_llm_executor_allows_invocation_timeout_override(self) -> None:
        telemetry = TelemetrySink(self.base / "telemetry")
        timeout_policy = ExecutionTimeoutPolicy(
            telemetry,
            min_seconds=1,
            max_seconds=1,
            multiplier=1.0,
        )
        executor = CommandLLMExecutor(
            "command",
            "sleep 2; printf 'done' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"",
            timeout_policy=timeout_policy,
        )

        result = executor.execute(
            LLMInvocation(
                task=self.task,
                run=self.run,
                prompt="Test timeout override",
                run_dir=self.base / "heartbeat",
                timeout_seconds_override=3,
            )
        )

        self.assertEqual("done", result.response_text)
        self.assertEqual(3, result.diagnostics["timeout_seconds"])

    def test_command_llm_executor_uses_absolute_prompt_paths_for_relative_run_dir(self) -> None:
        cwd = Path.cwd()
        os.chdir(self.base)
        self.addCleanup(lambda: os.chdir(cwd))

        executor = CommandLLMExecutor(
            "command",
            'cat < "$ACCRUVIA_LLM_PROMPT_PATH" > "$ACCRUVIA_LLM_RESPONSE_PATH"',
        )

        result = executor.execute(
            LLMInvocation(
                task=self.task,
                run=self.run,
                prompt="relative path prompt",
                run_dir=Path("relative-run"),
            )
        )

        self.assertEqual("relative path prompt", result.response_text)
        self.assertTrue(result.prompt_path.is_absolute())
        self.assertTrue(result.response_path.is_absolute())

    def test_agent_worker_marks_executor_bootstrap_failures_blocked(self) -> None:
        worker = AgentCommandWorker("exit 7")

        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("blocked", result.outcome)
        self.assertTrue(result.diagnostics["infrastructure_failure"])
        self.assertEqual("executor_process_failure", result.diagnostics["failure_category"])

    def test_shell_worker_emits_live_progress_for_long_running_child(self) -> None:
        progress_events: list[dict[str, object]] = []
        run_dir = self.base / "runs" / self.run.id
        run_dir.mkdir(parents=True)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text("plan\n", encoding="utf-8")
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(0.25)')}"
        worker = ShellCommandWorker(
            command,
            progress_callback=progress_events.append,
            status_interval_seconds=0.05,
            stale_after_seconds=1.0,
        )

        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("success", result.outcome)
        self.assertEqual("worker_launched", progress_events[0]["type"])
        status_events = [event for event in progress_events if event["type"] == "worker_status"]
        self.assertTrue(status_events)
        self.assertTrue(any(event["latest_artifact"] == "plan.txt" for event in status_events))
        self.assertTrue(all(not bool(event["stale"]) for event in status_events))

    def test_shell_worker_kills_stale_progress_before_full_run_timeout(self) -> None:
        progress_events: list[dict[str, object]] = []
        run_dir = self.base / "runs" / self.run.id
        run_dir.mkdir(parents=True)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text("plan\n", encoding="utf-8")
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(5)')}"
        worker = ShellCommandWorker(
            command,
            progress_callback=progress_events.append,
            status_interval_seconds=0.05,
            stale_after_seconds=0.05,
        )

        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("blocked", result.outcome)
        self.assertEqual("stale_progress_timeout", result.diagnostics["failure_category"])
        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual("stale_progress_timeout", report["failure_category"])
        self.assertTrue(any(event["type"] == "worker_status" and event["stale"] for event in progress_events))

    def test_select_worker_llm_command_prefers_selected_backend(self) -> None:
        backend, command = select_worker_llm_command(
            {
                "ACCRUVIA_WORKER_LLM_BACKEND": "claude",
                "ACCRUVIA_LLM_CODEX_COMMAND": "codex exec",
                "ACCRUVIA_LLM_CLAUDE_COMMAND": "claude",
            }
        )

        self.assertEqual("claude", backend)
        self.assertEqual("claude", command)

    def test_run_agent_worker_uses_shared_stdin_command_path(self) -> None:
        workspace = self.base / "workspace"
        tests_dir = workspace / "tests"
        tests_dir.mkdir(parents=True)
        for name in ("test_engine.py", "test_store.py", "test_validation.py", "test_phase1.py"):
            (tests_dir / name).write_text(
                "import unittest\n\n"
                "class Smoke(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
        subprocess.run(["git", "init", "-b", "main"], cwd=workspace, check=True, capture_output=True, text=True)
        cli_script = self.base / "fake_codex.sh"
        cli_script.write_text(
            "#!/usr/bin/env bash\n"
            "cat > shared_prompt.txt\n"
            "printf 'worker summary\\n'\n"
            "printf 'value = 1\\n' > changed_module.py\n",
            encoding="utf-8",
        )
        cli_script.chmod(0o755)
        run_dir = self.base / "run"
        result = run_agent_worker(
            {
                "ACCRUVIA_RUN_DIR": str(run_dir),
                "ACCRUVIA_PROJECT_WORKSPACE": str(workspace),
                "ACCRUVIA_TASK_ID": self.task.id,
                "ACCRUVIA_RUN_ID": self.run.id,
                "ACCRUVIA_TASK_OBJECTIVE": self.task.objective,
                "ACCRUVIA_RUN_SUMMARY": self.run.summary,
                "ACCRUVIA_TASK_STRATEGY": "default",
                "ACCRUVIA_WORKER_LLM_BACKEND": "codex",
                "ACCRUVIA_LLM_CODEX_COMMAND": str(cli_script),
            }
        )

        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(0, result)
        self.assertEqual("codex", report["llm_backend"])
        self.assertEqual("success", report["worker_outcome"])
        self.assertIn("changed_module.py", report["changed_files"])
        self.assertIn("Objective: Verify worker abstraction", (run_dir / "codex_worker_prompt.txt").read_text(encoding="utf-8"))
        self.assertIn("Objective: Verify worker abstraction", (workspace / "shared_prompt.txt").read_text(encoding="utf-8"))

    def test_run_agent_worker_fails_fast_when_focused_tests_exceed_timeout(self) -> None:
        workspace = self.base / "workspace"
        tests_dir = workspace / "tests"
        tests_dir.mkdir(parents=True)
        for name in ("test_store.py", "test_validation.py", "test_phase1.py"):
            (tests_dir / name).write_text(
                "import unittest\n\n"
                "class Smoke(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
        (tests_dir / "test_engine.py").write_text(
            "import time\n"
            "import unittest\n\n"
            "class Slow(unittest.TestCase):\n"
            "    def test_slow(self):\n"
            "        time.sleep(2)\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-b", "main"], cwd=workspace, check=True, capture_output=True, text=True)
        cli_script = self.base / "fake_codex.sh"
        cli_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'worker summary\\n'\n"
            "printf 'value = 1\\n' > changed_module.py\n",
            encoding="utf-8",
        )
        cli_script.chmod(0o755)
        run_dir = self.base / "run-timeout"

        result = run_agent_worker(
            {
                "ACCRUVIA_RUN_DIR": str(run_dir),
                "ACCRUVIA_PROJECT_WORKSPACE": str(workspace),
                "ACCRUVIA_TASK_ID": self.task.id,
                "ACCRUVIA_RUN_ID": self.run.id,
                "ACCRUVIA_TASK_OBJECTIVE": self.task.objective,
                "ACCRUVIA_RUN_SUMMARY": self.run.summary,
                "ACCRUVIA_TASK_STRATEGY": "default",
                "ACCRUVIA_WORKER_LLM_BACKEND": "codex",
                "ACCRUVIA_LLM_CODEX_COMMAND": str(cli_script),
                "ACCRUVIA_AGENT_TEST_TIMEOUT_SECONDS": "1",
            }
        )

        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(1, result)
        self.assertEqual("failed", report["worker_outcome"])
        self.assertEqual("validation_timeout", report["failure_category"])
        self.assertTrue(report["test_check"]["timed_out"])
        self.assertEqual(1, report["test_check"]["timeout_seconds"])
        self.assertIn("terminated", (run_dir / "test_output.txt").read_text(encoding="utf-8"))
        self.assertIn("changed_module.py", report["changed_files"])

    def test_focused_test_command_uses_lightweight_suite_for_repair_mode(self) -> None:
        self.assertEqual(
            ["python3", "-m", "unittest", "-v", "tests.test_workers"],
            _focused_test_command("lightweight_repair"),
        )

    def test_focused_test_command_uses_lightweight_suite_for_operator_mode(self) -> None:
        self.assertEqual(
            ["python3", "-m", "unittest", "-v", "tests.test_phase1"],
            _focused_test_command("lightweight_operator"),
        )

    def test_focused_test_command_keeps_default_suite_for_default_mode(self) -> None:
        self.assertEqual(
            [
                "python3",
                "-m",
                "unittest",
                "-v",
                "tests.test_engine",
                "tests.test_store",
                "tests.test_validation",
                "tests.test_phase1",
            ],
            _focused_test_command("default_focused"),
        )

    def test_run_agent_worker_fails_fast_when_validation_never_starts(self) -> None:
        workspace = self.base / "workspace-startup-timeout"
        workspace.mkdir(parents=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=workspace, check=True, capture_output=True, text=True)
        cli_script = self.base / "fake_codex_startup_timeout.sh"
        cli_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'worker summary\\n'\n"
            "printf 'value = 1\\n' > changed_module.py\n",
            encoding="utf-8",
        )
        cli_script.chmod(0o755)
        run_dir = self.base / "run-startup-timeout"

        with patch(
            "accruvia_harness.agent_worker._focused_test_command",
            return_value=["python3", "-c", "import time; time.sleep(2)"],
        ):
            result = run_agent_worker(
                {
                    "ACCRUVIA_RUN_DIR": str(run_dir),
                    "ACCRUVIA_PROJECT_WORKSPACE": str(workspace),
                    "ACCRUVIA_TASK_ID": self.task.id,
                    "ACCRUVIA_RUN_ID": self.run.id,
                    "ACCRUVIA_TASK_OBJECTIVE": self.task.objective,
                    "ACCRUVIA_RUN_SUMMARY": self.run.summary,
                    "ACCRUVIA_TASK_STRATEGY": "operator_ergonomics",
                    "ACCRUVIA_TASK_VALIDATION_MODE": "lightweight_operator",
                    "ACCRUVIA_WORKER_LLM_BACKEND": "codex",
                    "ACCRUVIA_LLM_CODEX_COMMAND": str(cli_script),
                    "ACCRUVIA_TASK_VALIDATION_STARTUP_TIMEOUT_SECONDS": "1",
                    "ACCRUVIA_AGENT_TEST_TIMEOUT_SECONDS": "5",
                }
            )

        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(1, result)
        self.assertEqual("validation_startup_timeout", report["failure_category"])
        self.assertTrue(report["test_check"]["timed_out"])
        self.assertEqual(1, report["test_check"]["startup_timeout_seconds"])
        self.assertIn("startup ceiling", (run_dir / "test_output.txt").read_text(encoding="utf-8"))

    def test_atomicity_gate_blocks_self_referential_operator_change_before_validation(self) -> None:
        workspace = self.base / "workspace-self-ref"
        target = workspace / "src" / "accruvia_harness"
        target.mkdir(parents=True)
        (target / "agent_worker.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=workspace, check=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        cli_script = self.base / "fake_codex_self_ref.sh"
        cli_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'worker summary\\n'\n"
            "printf 'VALUE = 2\\n' > src/accruvia_harness/agent_worker.py\n",
            encoding="utf-8",
        )
        cli_script.chmod(0o755)
        run_dir = self.base / "run-self-ref"

        result = run_agent_worker(
            {
                "ACCRUVIA_RUN_DIR": str(run_dir),
                "ACCRUVIA_PROJECT_WORKSPACE": str(workspace),
                "ACCRUVIA_TASK_ID": self.task.id,
                "ACCRUVIA_TASK_TITLE": "Operator validation self reference",
                "ACCRUVIA_RUN_ID": self.run.id,
                "ACCRUVIA_RUN_ATTEMPT": "1",
                "ACCRUVIA_TASK_OBJECTIVE": self.task.objective,
                "ACCRUVIA_RUN_SUMMARY": self.run.summary,
                "ACCRUVIA_TASK_STRATEGY": "operator_ergonomics",
                "ACCRUVIA_TASK_VALIDATION_MODE": "lightweight_operator",
                "ACCRUVIA_WORKER_LLM_BACKEND": "codex",
                "ACCRUVIA_LLM_CODEX_COMMAND": str(cli_script),
            }
        )

        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(1, result)
        self.assertEqual("blocked", report["worker_outcome"])
        self.assertEqual("policy_self_modification", report["failure_category"])
        self.assertEqual("block_self_referential", report["atomicity_gate"]["action"])
        self.assertTrue((run_dir / "atomicity_telemetry.json").exists())
        self.assertFalse((run_dir / "test_output.txt").exists())

    def test_atomicity_gate_narrows_default_validation_for_operator_surface(self) -> None:
        workspace = self.base / "workspace-narrow"
        src = workspace / "src" / "accruvia_harness" / "commands"
        tests_dir = workspace / "tests"
        src.mkdir(parents=True)
        tests_dir.mkdir(parents=True)
        (src / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
        (tests_dir / "test_cli.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (tests_dir / "test_phase1.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-b", "main"], cwd=workspace, check=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        cli_script = self.base / "fake_codex_narrow.sh"
        cli_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'worker summary\\n'\n"
            "printf 'VALUE = 2\\n' > src/accruvia_harness/commands/core.py\n",
            encoding="utf-8",
        )
        cli_script.chmod(0o755)
        run_dir = self.base / "run-narrow"

        result = run_agent_worker(
            {
                "ACCRUVIA_RUN_DIR": str(run_dir),
                "ACCRUVIA_PROJECT_WORKSPACE": str(workspace),
                "ACCRUVIA_TASK_ID": self.task.id,
                "ACCRUVIA_TASK_TITLE": "Operator startup wording",
                "ACCRUVIA_RUN_ID": self.run.id,
                "ACCRUVIA_RUN_ATTEMPT": "2",
                "ACCRUVIA_TASK_OBJECTIVE": "Adjust supervise startup wording for operators",
                "ACCRUVIA_RUN_SUMMARY": "Previous validation timed out.",
                "ACCRUVIA_TASK_STRATEGY": "operator_ergonomics",
                "ACCRUVIA_TASK_VALIDATION_MODE": "default_focused",
                "ACCRUVIA_WORKER_LLM_BACKEND": "codex",
                "ACCRUVIA_LLM_CODEX_COMMAND": str(cli_script),
            }
        )

        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(0, result)
        self.assertEqual("validate_narrow", report["atomicity_gate"]["action"])
        self.assertEqual("lightweight_operator", report["test_check"]["selection"])

    def test_build_worker_from_config_defaults_agent_worker_command(self) -> None:
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
            worker_backend="agent",
            worker_command=None,
            llm_backend="codex",
            llm_model=None,
            llm_command=None,
            llm_codex_command="codex exec",
            llm_claude_command=None,
            llm_accruvia_client_command=None,
        )

        worker = build_worker_from_config(config)

        self.assertIsInstance(worker, AgentCommandWorker)
        self.assertEqual(_default_agent_worker_command(), worker.command)

    def test_build_llm_router_uses_higher_memory_floor_for_llm_clis(self) -> None:
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
            llm_model=None,
            llm_command=None,
            llm_codex_command="printf 'ok' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"",
            llm_claude_command=None,
            llm_accruvia_client_command=None,
            memory_limit_mb=1024,
        )

        router = build_llm_router(config)
        executor, _ = router.resolve()

        self.assertIsNone(executor.resource_policy.memory_limit_mb)

    def test_agent_worker_uses_same_higher_memory_floor_for_llm_clis(self) -> None:
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
            worker_backend="agent",
            worker_command="printf ok",
            llm_backend="codex",
            llm_model=None,
            llm_command=None,
            llm_codex_command="codex exec",
            llm_claude_command=None,
            llm_accruvia_client_command=None,
            memory_limit_mb=1024,
        )

        worker = build_worker_from_config(config)

        self.assertIsInstance(worker, AgentCommandWorker)
        self.assertIsNone(worker.resource_policy.memory_limit_mb)

    def test_resolve_memory_limit_disables_cap_when_large_heap_floor_exceeds_machine_budget(self) -> None:
        with patch("accruvia_harness.resource_limits._total_memory_mb", return_value=3072):
            memory_limit_mb = resolve_memory_limit_mb(1024, backend_names=("codex",))

        self.assertIsNone(memory_limit_mb)

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

    def test_llm_worker_ignores_non_numeric_metadata_values(self) -> None:
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
            llm_codex_command="printf 'codex response' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"; printf '{\"cost_usd\": \"error\", \"prompt_tokens\": \"oops\"}' > \"$ACCRUVIA_LLM_METADATA_PATH\"",
            llm_claude_command=None,
            llm_accruvia_client_command=None,
        )

        telemetry = TelemetrySink(self.base / "telemetry")
        worker = build_worker_from_config(config, telemetry=telemetry)
        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("success", result.outcome)
        summary = telemetry.summary()
        self.assertEqual(0.0, summary["cost_totals"]["cost_usd"])

    def test_llm_worker_falls_back_to_next_backend_when_primary_fails(self) -> None:
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
            llm_backend="claude",
            llm_model="sonnet",
            llm_command=None,
            llm_codex_command="printf 'codex response' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"",
            llm_claude_command="printf 'auth outage' >&2; exit 9",
            llm_accruvia_client_command=None,
        )

        telemetry = TelemetrySink(self.base / "telemetry")
        worker = build_worker_from_config(config, telemetry=telemetry)
        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("success", result.outcome)
        self.assertEqual("codex", result.diagnostics["llm_backend"])
        summary = telemetry.summary()
        self.assertTrue(any(item["category"] == "llm_executor_failure" for item in summary["warnings"]))

    def test_llm_worker_timeout_handles_bytes_stderr_cleanly(self) -> None:
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
            llm_codex_command="python3 - <<'PY'\nimport sys, time\nsys.stderr.buffer.write(b'partial stderr')\ntime.sleep(2)\nPY",
            llm_claude_command=None,
            llm_accruvia_client_command=None,
            timeout_min_seconds=1,
            timeout_max_seconds=1,
            timeout_multiplier=1.0,
        )

        worker = build_worker_from_config(config, telemetry=TelemetrySink(self.base / "telemetry"))
        result = worker.work(self.task, self.run, self.base)

        self.assertEqual("blocked", result.outcome)
        error_path = self.base / "runs" / self.run.id / "llm_error.txt"
        self.assertTrue(error_path.exists())

    def test_parse_affirmation_response_handles_loose_rejection_text(self) -> None:
        approved, rationale = parse_affirmation_response("I would reject this candidate.\nIt is not ready to promote.")
        self.assertFalse(approved)
        self.assertIn("not ready", rationale)

    def test_parse_affirmation_response_handles_structured_text_fields(self) -> None:
        approved, rationale = parse_affirmation_response(
            "decision: approved\nrationale: deterministic gates passed and risk is acceptable"
        )
        self.assertTrue(approved)
        self.assertIn("rationale", rationale)

    def test_parse_affirmation_response_handles_json_fenced_payload(self) -> None:
        approved, rationale = parse_affirmation_response(
            '```json\n{"decision":"rejected","rationale":"report shows failing tests"}\n```'
        )
        self.assertFalse(approved)
        self.assertIn("failing tests", rationale)

    def test_build_subprocess_env_sanitizes_ambient_variables(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SECRET_TOKEN": "shh",
                "PATH": "/usr/bin",
                "KEEP_ME": "ok",
                "ACCRUVIA_SECRET_TOKEN": "dont-pass",
            },
            clear=True,
        ):
            env = build_subprocess_env({"ACCRUVIA_TASK_ID": "task_1"}, passthrough=("KEEP_ME",))

        self.assertNotIn("SECRET_TOKEN", env)
        self.assertNotIn("ACCRUVIA_SECRET_TOKEN", env)
        self.assertEqual("/usr/bin", env["PATH"])
        self.assertEqual("ok", env["KEEP_ME"])
        self.assertEqual("task_1", env["ACCRUVIA_TASK_ID"])
