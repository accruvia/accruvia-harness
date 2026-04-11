from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accruvia_harness.atomicity import atomicity_gate, changed_files
from accruvia_harness.adapters import build_adapter_registry
from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import Run, RunStatus, Task, new_id
from accruvia_harness.llm import CommandLLMExecutor, LLMInvocation, build_llm_router, parse_affirmation_response
from accruvia_harness.resource_limits import resolve_memory_limit_mb
from accruvia_harness.subprocess_env import build_subprocess_env
from accruvia_harness.telemetry import TelemetrySink
from accruvia_harness.timeout_policy import ExecutionTimeoutPolicy
from accruvia_harness.workers import LocalArtifactWorker


def _minimal_config(base: Path, **overrides) -> HarnessConfig:
    defaults = dict(
        db_path=base / "harness.db",
        workspace_root=base,
        log_path=base / "harness.log",
        default_project_name="demo",
        default_repo="accruvia/accruvia",
        runtime_backend="local",
        temporal_target="localhost:7233",
        temporal_namespace="default",
        temporal_task_queue="accruvia-harness",
        llm_backend="auto",
        llm_model=None,
        llm_command=None,
        llm_codex_command=None,
        llm_claude_command=None,
        llm_accruvia_client_command=None,
    )
    defaults.update(overrides)
    return HarnessConfig(**defaults)


class LocalWorkerTests(unittest.TestCase):
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


class LLMExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.task = Task(
            id=new_id("task"),
            project_id="project_1",
            title="LLM task",
            objective="Verify CommandLLMExecutor",
        )
        self.run = Run(
            id=new_id("run"),
            task_id=self.task.id,
            status=RunStatus.WORKING,
            attempt=1,
            summary="",
        )

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


class LLMRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)

    def test_build_llm_router_uses_higher_memory_floor_for_llm_clis(self) -> None:
        config = _minimal_config(
            self.base,
            llm_backend="codex",
            llm_codex_command="printf 'ok' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"",
            memory_limit_mb=1024,
        )

        router = build_llm_router(config)
        executor, _ = router.resolve()

        self.assertIsNone(executor.resource_policy.memory_limit_mb)

    def test_resolve_memory_limit_disables_cap_when_large_heap_floor_exceeds_machine_budget(self) -> None:
        with patch("accruvia_harness.resource_limits._total_memory_mb", return_value=3072):
            memory_limit_mb = resolve_memory_limit_mb(1024, backend_names=("codex",))

        self.assertIsNone(memory_limit_mb)

    def test_llm_router_prefers_accruvia_client_in_github_actions(self) -> None:
        config = _minimal_config(
            self.base,
            llm_backend="auto",
            llm_codex_command="printf 'codex response' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"",
            llm_accruvia_client_command="printf 'accruvia response' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"",
        )

        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=False):
            executor, backend = build_llm_router(config).resolve()

        self.assertEqual("accruvia_client", backend)
        self.assertEqual("accruvia_client", executor.backend_name)


class AffirmationParserTests(unittest.TestCase):
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


class SubprocessEnvTests(unittest.TestCase):
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


class ChangedFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)

    def test_changed_files_detects_committed_changes(self) -> None:
        workspace = self.base / "workspace-committed"
        workspace.mkdir(parents=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=workspace, check=True, capture_output=True, text=True)
        (workspace / "initial.txt").write_text("init\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
            cwd=workspace, check=True, capture_output=True, text=True,
        )
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=workspace, check=True, capture_output=True, text=True)
        (workspace / "committed_file.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "add file"],
            cwd=workspace, check=True, capture_output=True, text=True,
        )
        files = changed_files(workspace)
        self.assertIn("committed_file.txt", files)


class AgentBackendRemovalAsserts(unittest.TestCase):
    def test_agent_worker_module_is_gone(self):
        with self.assertRaises(ModuleNotFoundError):
            import accruvia_harness.agent_worker  # noqa: F401

    def test_agent_command_worker_class_is_gone(self):
        from accruvia_harness import workers
        self.assertFalse(hasattr(workers, "AgentCommandWorker"))

    def test_shell_command_worker_class_is_gone(self):
        from accruvia_harness import workers
        self.assertFalse(hasattr(workers, "ShellCommandWorker"))

    def test_llm_task_worker_class_is_gone(self):
        from accruvia_harness import workers
        self.assertFalse(hasattr(workers, "LLMTaskWorker"))

    def test_command_worker_base_is_gone(self):
        from accruvia_harness import workers
        self.assertFalse(hasattr(workers, "CommandWorker"))

    def test_codex_worker_script_is_gone(self):
        self.assertFalse(Path("bin/accruvia-codex-worker").exists())

    def test_config_rejects_worker_backend_field(self):
        import tempfile as _tempfile
        import os as _os
        from accruvia_harness.config import load_persisted_config
        with _tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"worker_backend": "agent"}, f)
            path = f.name
        try:
            with self.assertRaisesRegex(ValueError, "removed in pre-alpha"):
                load_persisted_config(path)
        finally:
            _os.unlink(path)


if __name__ == "__main__":
    unittest.main()
