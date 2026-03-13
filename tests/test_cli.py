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

    def run_raw(
        self,
        *args: str,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *args],
            cwd=self.repo_root,
            env=env or self.env,
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
        )

    def run_cli(self, *args: str) -> dict[str, object]:
        completed = self.run_raw("-m", "accruvia_harness", "--json", *args)
        if completed.returncode != 0:
            raise subprocess.CalledProcessError(
                completed.returncode,
                completed.args,
                output=completed.stdout,
                stderr=completed.stderr,
            )
        return json.loads(completed.stdout)

    def test_package_import_does_not_require_optional_routing_service(self) -> None:
        completed = self.run_raw("-c", "import accruvia_harness; print('ok')")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("ok", completed.stdout.strip())

    def test_status_command_runs_with_only_harness_on_pythonpath(self) -> None:
        completed = self.run_raw("-m", "accruvia_harness", "--json", "status")

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual([], payload["projects"])
        self.assertEqual([], payload["tasks"])

    def test_doctor_reports_missing_llm_executor_by_default(self) -> None:
        payload = self.run_cli("doctor")

        self.assertFalse(payload["heartbeats_ready"])
        self.assertTrue(payload["readiness"]["inspection_ready"])
        self.assertTrue(payload["readiness"]["task_execution_ready"])
        self.assertFalse(payload["readiness"]["autonomous_ready"])
        self.assertEqual("prototype", payload["prototype"]["stage"])
        self.assertIn("No LLM executor is configured.", payload["issues"])

    def test_configure_llm_persists_executor_settings_across_fresh_sessions(self) -> None:
        llm_script = Path(self.temp_dir.name) / "persisted_llm.sh"
        llm_script.write_text("#!/usr/bin/env bash\nprintf 'ok\\n' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n", encoding="utf-8")
        llm_script.chmod(0o755)

        configure = self.run_raw(
            "-m",
            "accruvia_harness",
            "--json",
            "configure-llm",
            "--backend",
            "command",
            "--command",
            str(llm_script),
        )
        self.assertEqual(0, configure.returncode, configure.stderr)

        fresh_env = self.env.copy()
        fresh_env.pop("ACCRUVIA_LLM_BACKEND", None)
        fresh_env.pop("ACCRUVIA_LLM_COMMAND", None)
        config = self.run_raw("-m", "accruvia_harness", "--json", "config", env=fresh_env)

        self.assertEqual(0, config.returncode, config.stderr)
        payload = json.loads(config.stdout)
        self.assertEqual("command", payload["llm_backend"])
        self.assertTrue(payload["llm_command"].endswith("persisted_llm.sh [REDACTED]"))

    def test_setup_autodetects_codex_and_persists_it(self) -> None:
        fake_bin = Path(self.temp_dir.name) / "bin"
        fake_bin.mkdir()
        codex = fake_bin / "codex"
        codex.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = \"exec\" ]; then\n"
            "  printf 'ok\\n' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n"
            "  exit 0\n"
            "fi\n"
            "echo codex 1.0\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        setup_env = self.env.copy()
        setup_env["PATH"] = f"{fake_bin}:{setup_env.get('PATH', '')}"

        completed = self.run_raw("-m", "accruvia_harness", "--json", "setup", "--yes", env=setup_env)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["configured"])
        self.assertEqual("codex", payload["selected"]["backend"])
        self.assertTrue(payload["probe"]["ok"])
        self.assertTrue(payload["doctor"]["heartbeats_ready"])
        self.assertIn("smoke-test", payload["next_steps"][1])

        fresh_config = self.run_raw("-m", "accruvia_harness", "--json", "config", env=setup_env)
        self.assertEqual(0, fresh_config.returncode, fresh_config.stderr)
        config_payload = json.loads(fresh_config.stdout)
        self.assertEqual("codex", config_payload["llm_backend"])
        self.assertEqual("codex [REDACTED]", config_payload["llm_codex_command"])

    def test_setup_explains_why_an_llm_provider_is_required(self) -> None:
        fake_bin = Path(self.temp_dir.name) / "bin"
        fake_bin.mkdir()
        codex = fake_bin / "codex"
        codex.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'setup ok\\n' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        setup_env = self.env.copy()
        setup_env["PATH"] = f"{fake_bin}:{setup_env.get('PATH', '')}"

        completed = self.run_raw(
            "-m",
            "accruvia_harness",
            "setup",
            env=setup_env,
            input_text="1\n\n",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("needs at least one working LLM provider", completed.stderr)
        self.assertIn("Installed providers detected on PATH", completed.stderr)
        self.assertNotIn("env vars to pass through", completed.stderr)

    def test_single_detected_provider_is_auto_configured_for_heartbeat_commands(self) -> None:
        fake_bin = Path(self.temp_dir.name) / "bin"
        fake_bin.mkdir()
        codex = fake_bin / "codex"
        codex.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = \"exec\" ]; then\n"
            "  printf '{\"summary\":\"Bootstrap backlog\",\"priority_focus\":\"bootstrap\",\"issue_creation_needed\":true,"
            "\\\"proposed_tasks\\\":[{\\\"title\\\":\\\"Bootstrap task\\\",\\\"objective\\\":\\\"Create the first task\\\",\\\"priority\\\":180,\\\"rationale\\\":\\\"Start the loop\\\"}]}'\n"
            "  exit 0\n"
            "fi\n"
            "exit 2\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        env = self.env.copy()
        env["PATH"] = f"{fake_bin}:/usr/bin:/bin"

        completed = self.run_raw("-m", "accruvia_harness", "--json", "create-project", "auto", "auto project", env=env)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertIn("heartbeat", payload)

        config = self.run_raw("-m", "accruvia_harness", "--json", "config", env=env)
        self.assertEqual(0, config.returncode, config.stderr)
        config_payload = json.loads(config.stdout)
        self.assertEqual("codex", config_payload["llm_backend"])
        self.assertEqual("codex [REDACTED]", config_payload["llm_codex_command"])

    def test_supervise_refreshes_detected_provider_command_on_startup(self) -> None:
        fake_bin = Path(self.temp_dir.name) / "bin"
        fake_bin.mkdir()
        codex = fake_bin / "codex"
        codex.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = \"exec\" ]; then\n"
            "  printf '{\"summary\":\"No new work\",\"priority_focus\":\"none\",\"issue_creation_needed\":false,\"proposed_tasks\":[]}'\n"
            "  exit 0\n"
            "fi\n"
            "exit 2\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        env = self.env.copy()
        env["PATH"] = f"{fake_bin}:/usr/bin:/bin"

        configured = self.run_raw(
            "-m",
            "accruvia_harness",
            "configure-llm",
            "--backend",
            "codex",
            "--codex-command",
            'codex exec < "$ACCRUVIA_LLM_PROMPT_PATH" > "$ACCRUVIA_LLM_RESPONSE_PATH"',
            env=env,
        )
        self.assertEqual(0, configured.returncode, configured.stderr)

        project = json.loads(
            self.run_raw(
                "-m",
                "accruvia_harness",
                "--json",
                "create-project",
                "startup-refresh",
                "startup refresh project",
                "--no-bootstrap-heartbeat",
                env=env,
            ).stdout
        )["project"]

        completed = self.run_raw(
            "-m",
            "accruvia_harness",
            "--json",
            "supervise",
            "--project-id",
            project["id"],
            "--one-shot",
            env=env,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(1, payload["heartbeat_count"])

        config_path = Path(self.temp_dir.name) / "config.json"
        persisted = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual("codex exec", persisted["llm_codex_command"])

    def test_auto_configure_rejects_hanging_detected_provider(self) -> None:
        fake_bin = Path(self.temp_dir.name) / "bin"
        fake_bin.mkdir()
        codex = fake_bin / "codex"
        codex.write_text(
            "#!/usr/bin/env bash\n"
            "sleep 30 &\n"
            "wait\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        env = self.env.copy()
        env["PATH"] = f"{fake_bin}:/usr/bin:/bin"

        project_payload = self.run_raw(
            "-m",
            "accruvia_harness",
            "--json",
            "create-project",
            "probe-timeout",
            "probe timeout project",
            "--no-bootstrap-heartbeat",
            env=env,
        )
        self.assertEqual(0, project_payload.returncode, project_payload.stderr)
        project = json.loads(project_payload.stdout)["project"]

        completed = self.run_raw("-m", "accruvia_harness", "heartbeat", project["id"], env=env)

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("detected command is not ready", completed.stderr)
        self.assertIn("did not finish within 5 seconds", completed.stderr)

    def test_multiple_detected_providers_require_explicit_choice_in_noninteractive_mode(self) -> None:
        fake_bin = Path(self.temp_dir.name) / "bin"
        fake_bin.mkdir()
        for name in ("codex", "claude"):
            path = fake_bin / name
            path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        env = self.env.copy()
        env["PATH"] = str(fake_bin)

        completed = self.run_raw("-m", "accruvia_harness", "heartbeat", "project_123", env=env)

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("multiple installed providers", completed.stderr)

    def test_reset_local_state_requires_explicit_confirmation(self) -> None:
        completed = self.run_raw("-m", "accruvia_harness", "reset-local-state")

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("--yes", completed.stderr)

    def test_reset_local_state_can_preserve_config(self) -> None:
        llm_script = Path(self.temp_dir.name) / "persisted_llm.sh"
        llm_script.write_text("#!/usr/bin/env bash\nprintf 'ok\\n' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n", encoding="utf-8")
        llm_script.chmod(0o755)
        self.run_raw(
            "-m",
            "accruvia_harness",
            "--json",
            "configure-llm",
            "--backend",
            "command",
            "--command",
            str(llm_script),
        )
        payload = self.run_cli("reset-local-state", "--yes", "--keep-config")

        self.assertTrue(payload["reset"])
        self.assertTrue(any(item.endswith("config.json") for item in payload["preserved"]))

        doctor = self.run_cli("doctor")
        self.assertTrue(doctor["config_file"]["exists"])
        self.assertTrue(doctor["database"]["exists"])
        self.assertTrue(doctor["heartbeats_ready"])

    def test_doctor_is_human_readable_by_default(self) -> None:
        completed = self.run_raw("-m", "accruvia_harness", "doctor")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Accruvia Harness doctor", completed.stdout)
        self.assertIn("Readiness", completed.stdout)
        self.assertIn("Detected on PATH", completed.stdout)
        self.assertFalse(completed.stdout.lstrip().startswith("{"))

    def test_smoke_test_is_repeatable_in_same_harness_home(self) -> None:
        first = self.run_raw("-m", "accruvia_harness", "--json", "smoke-test")
        second = self.run_raw("-m", "accruvia_harness", "--json", "smoke-test")

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        first_payload = json.loads(first.stdout)
        second_payload = json.loads(second.stdout)
        self.assertEqual("smoke-project", first_payload["project"]["name"])
        self.assertEqual(first_payload["project"]["id"], second_payload["project"]["id"])
        self.assertEqual("completed", first_payload["task"]["status"])
        self.assertEqual("completed", second_payload["task"]["status"])

    def test_smoke_test_is_human_readable_by_default(self) -> None:
        completed = self.run_raw("-m", "accruvia_harness", "smoke-test")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Smoke test complete", completed.stdout)
        self.assertIn("Next step", completed.stdout)
        self.assertFalse(completed.stdout.lstrip().startswith("{"))

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

    def test_process_next_honors_explicit_lightweight_validation_mode(self) -> None:
        codex_script = Path(self.temp_dir.name) / "fake_codex_validation_mode.sh"
        codex_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'lightweight summary\\n'\n"
            "printf 'value = 1\\n' > lightweight_module.py\n",
            encoding="utf-8",
        )
        codex_script.chmod(0o755)
        self.env["ACCRUVIA_WORKER_BACKEND"] = "agent"
        self.env["ACCRUVIA_LLM_BACKEND"] = "codex"
        self.env["ACCRUVIA_LLM_CODEX_COMMAND"] = str(codex_script)

        project = self.run_cli("create-project", "lightweight-mode", "lightweight mode project")["project"]
        task = self.run_cli(
            "create-task",
            project["id"],
            "Light repair task",
            "Verify explicit validation mode flows through process-next",
            "--validation-mode",
            "lightweight_repair",
            "--strategy",
            "default",
        )["task"]

        self.run_cli("process-next", "--project-id", project["id"], "--worker-id", "cli-worker", "--lease-seconds", "60")
        report = self.run_cli("task-report", task["id"])
        artifact_report = next(item for item in report["runs"][0]["artifacts"] if item["kind"] == "report")
        payload = json.loads(Path(artifact_report["path"]).read_text(encoding="utf-8"))

        self.assertEqual("lightweight_repair", payload["validation_mode"])
        self.assertEqual(["python3", "-m", "unittest", "-v", "tests.test_workers"], payload["test_check"]["command"])
        self.assertEqual("lightweight_repair", payload["test_check"]["selection"])

    def test_create_project_runs_bootstrap_heartbeat_by_default(self) -> None:
        llm_script = Path(self.temp_dir.name) / "fake_bootstrap_heartbeat.sh"
        llm_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"summary\":\"Bootstrap backlog\",\"priority_focus\":\"bootstrap\",\"issue_creation_needed\":true,"
            "\\\"proposed_tasks\\\":[{\\\"title\\\":\\\"Bootstrap task\\\",\\\"objective\\\":\\\"Create the first task\\\",\\\"priority\\\":180,\\\"rationale\\\":\\\"Start the loop\\\"}]}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        llm_script.chmod(0o755)
        self.env["ACCRUVIA_LLM_BACKEND"] = "command"
        self.env["ACCRUVIA_LLM_COMMAND"] = str(llm_script)

        payload = self.run_cli("create-project", "bootstrap", "bootstrap project")
        summary = self.run_cli("summary", "--project-id", payload["project"]["id"])

        self.assertIn("heartbeat", payload)
        self.assertEqual(1, len(payload["heartbeat"]["created_tasks"]))
        self.assertEqual("Bootstrap task", payload["heartbeat"]["created_tasks"][0]["title"])
        self.assertEqual(1, summary["metrics"]["tasks_by_status"]["pending"])

    def test_supervise_auto_heartbeats_project_when_queue_is_empty(self) -> None:
        llm_script = Path(self.temp_dir.name) / "fake_supervise_heartbeat.sh"
        llm_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"summary\":\"Create initial work\",\"priority_focus\":\"bootstrap\",\"issue_creation_needed\":true,"
            "\\\"proposed_tasks\\\":[{\\\"title\\\":\\\"Initial autonomous task\\\",\\\"objective\\\":\\\"Create and complete the first task\\\",\\\"priority\\\":210,\\\"rationale\\\":\\\"Seed the project backlog\\\"}]}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        llm_script.chmod(0o755)
        self.env["ACCRUVIA_LLM_BACKEND"] = "command"
        self.env["ACCRUVIA_LLM_COMMAND"] = str(llm_script)

        project = self.run_cli(
            "create-project",
            "auto-heartbeat",
            "auto heartbeat project",
            "--no-bootstrap-heartbeat",
        )["project"]

        result = self.run_cli("supervise", "--project-id", project["id"], "--worker-id", "supervisor-a", "--one-shot")
        summary = self.run_cli("summary", "--project-id", project["id"])

        self.assertEqual(1, result["heartbeat_count"])
        self.assertEqual([project["id"]], result["heartbeat_project_ids"])
        self.assertEqual(1, result["processed_count"])
        self.assertEqual(1, summary["metrics"]["tasks_by_status"]["completed"])

    def test_supervise_heartbeats_before_processing_existing_backlog(self) -> None:
        llm_script = Path(self.temp_dir.name) / "fake_supervise_ordering_heartbeat.sh"
        llm_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"summary\":\"Create urgent work\",\"priority_focus\":\"bootstrap\",\"issue_creation_needed\":true,"
            "\\\"proposed_tasks\\\":[{\\\"title\\\":\\\"Urgent heartbeat task\\\",\\\"objective\\\":\\\"Run before queued work\\\",\\\"priority\\\":500,\\\"rationale\\\":\\\"New critical context from heartbeat\\\"}]}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        llm_script.chmod(0o755)
        self.env["ACCRUVIA_LLM_BACKEND"] = "command"
        self.env["ACCRUVIA_LLM_COMMAND"] = str(llm_script)

        project = self.run_cli(
            "create-project",
            "heartbeat-ordering",
            "heartbeat ordering project",
            "--no-bootstrap-heartbeat",
        )["project"]
        low = self.run_cli(
            "create-task",
            project["id"],
            "Low queued task",
            "Queued before supervise",
            "--priority",
            "100",
        )["task"]

        result = self.run_cli("supervise", "--project-id", project["id"], "--worker-id", "supervisor-a", "--one-shot")
        report = self.run_cli("ops-report")

        self.assertEqual(1, result["heartbeat_count"])
        self.assertEqual(2, result["processed_count"])
        self.assertEqual(project["id"], result["heartbeat_project_ids"][0])
        self.assertNotEqual(low["id"], result["processed_task_ids"][0])
        self.assertEqual(low["id"], result["processed_task_ids"][1])

    def test_heartbeat_processes_created_tasks_by_default(self) -> None:
        llm_script = Path(self.temp_dir.name) / "fake_direct_heartbeat.sh"
        llm_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"summary\":\"Create follow-on work\",\"priority_focus\":\"loop continuity\",\"issue_creation_needed\":true,"
            "\\\"proposed_tasks\\\":[{\\\"title\\\":\\\"Heartbeat task\\\",\\\"objective\\\":\\\"Complete the task immediately after heartbeat\\\",\\\"priority\\\":220,\\\"rationale\\\":\\\"Keep the loop moving\\\"}]}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        llm_script.chmod(0o755)
        self.env["ACCRUVIA_LLM_BACKEND"] = "command"
        self.env["ACCRUVIA_LLM_COMMAND"] = str(llm_script)

        project = self.run_cli(
            "create-project",
            "heartbeat-default",
            "heartbeat default processing project",
            "--no-bootstrap-heartbeat",
        )["project"]

        payload = self.run_cli("heartbeat", project["id"], "--worker-id", "heartbeat-a")
        summary = self.run_cli("summary", "--project-id", project["id"])

        self.assertEqual(1, len(payload["heartbeat"]["created_tasks"]))
        self.assertEqual(1, payload["processing"]["processed_count"])
        self.assertEqual(1, summary["metrics"]["tasks_by_status"]["completed"])

    def test_heartbeat_can_leave_created_tasks_pending_when_opted_out(self) -> None:
        llm_script = Path(self.temp_dir.name) / "fake_direct_heartbeat_opt_out.sh"
        llm_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"summary\":\"Create deferred work\",\"priority_focus\":\"operator inspection\",\"issue_creation_needed\":true,"
            "\\\"proposed_tasks\\\":[{\\\"title\\\":\\\"Deferred heartbeat task\\\",\\\"objective\\\":\\\"Leave the task pending when requested\\\",\\\"priority\\\":220,\\\"rationale\\\":\\\"Allow inspection before execution\\\"}]}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        llm_script.chmod(0o755)
        self.env["ACCRUVIA_LLM_BACKEND"] = "command"
        self.env["ACCRUVIA_LLM_COMMAND"] = str(llm_script)

        project = self.run_cli(
            "create-project",
            "heartbeat-opt-out",
            "heartbeat opt out project",
            "--no-bootstrap-heartbeat",
        )["project"]

        payload = self.run_cli("heartbeat", project["id"], "--no-process-created-tasks")
        summary = self.run_cli("summary", "--project-id", project["id"])

        self.assertEqual(1, len(payload["heartbeat"]["created_tasks"]))
        self.assertNotIn("processing", payload)
        self.assertEqual(1, summary["metrics"]["tasks_by_status"]["pending"])

    def test_nudge_project_records_operator_note_and_processes_heartbeat_tasks(self) -> None:
        llm_script = Path(self.temp_dir.name) / "fake_nudge_heartbeat.sh"
        llm_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"summary\":\"Respond to operator nudge\",\"priority_focus\":\"onboarding\",\"issue_creation_needed\":true,"
            "\\\"proposed_tasks\\\":[{\\\"title\\\":\\\"Nudged task\\\",\\\"objective\\\":\\\"Act on the operator note\\\",\\\"priority\\\":220,\\\"rationale\\\":\\\"The nudge should shape the next heartbeat\\\"}]}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        llm_script.chmod(0o755)
        self.env["ACCRUVIA_LLM_BACKEND"] = "command"
        self.env["ACCRUVIA_LLM_COMMAND"] = str(llm_script)

        project = self.run_cli(
            "create-project",
            "nudge-project",
            "nudge project",
            "--no-bootstrap-heartbeat",
        )["project"]

        payload = self.run_cli(
            "nudge-project",
            project["id"],
            "Pay extra attention to onboarding and DX",
        )
        context = self.run_cli("context-packet", "--project-id", project["id"])
        summary = self.run_cli("summary", "--project-id", project["id"])

        self.assertEqual("Pay extra attention to onboarding and DX", payload["nudge"]["payload"]["note"])
        self.assertEqual(1, len(payload["heartbeat"]["created_tasks"]))
        self.assertEqual(1, payload["processing"]["processed_count"])
        self.assertEqual(
            "Pay extra attention to onboarding and DX",
            context["operator_nudges"][0]["note"],
        )
        self.assertEqual(1, summary["metrics"]["tasks_by_status"]["completed"])

    def test_update_project_persists_repo_and_policy_settings(self) -> None:
        project = self.run_cli("create-project", "demo", "demo project")["project"]

        updated = self.run_cli(
            "update-project",
            project["id"],
            "--repo-provider",
            "github",
            "--repo-name",
            "accruvia/routellect",
            "--promotion-mode",
            "branch_and_pr",
            "--workspace-policy",
            "isolated_required",
            "--base-branch",
            "main",
        )["project"]
        status = self.run_cli("status")

        self.assertEqual("github", updated["repo_provider"])
        self.assertEqual("accruvia/routellect", updated["repo_name"])
        self.assertEqual("branch_and_pr", updated["promotion_mode"])
        self.assertEqual("isolated_required", updated["workspace_policy"])
        self.assertEqual("main", updated["base_branch"])
        self.assertEqual("accruvia/routellect", status["projects"][0]["repo_name"])

    def test_project_name_can_be_used_where_project_id_is_expected(self) -> None:
        llm_script = Path(self.temp_dir.name) / "named_project_heartbeat.sh"
        llm_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"summary\":\"No backlog changes\",\"priority_focus\":\"none\",\"issue_creation_needed\":false,\"proposed_tasks\":[]}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        llm_script.chmod(0o755)
        self.env["ACCRUVIA_LLM_BACKEND"] = "command"
        self.env["ACCRUVIA_LLM_COMMAND"] = str(llm_script)

        project = self.run_cli(
            "create-project",
            "accruvia-harness",
            "self hosting project",
            "--no-bootstrap-heartbeat",
        )["project"]

        task = self.run_cli(
            "create-task",
            "accruvia-harness",
            "Named project task",
            "Prove project-name resolution works",
        )["task"]
        summary = self.run_cli("summary", "--project-id", "accruvia-harness")
        result = self.run_cli("supervise", "--project-id", "accruvia-harness", "--one-shot")

        self.assertEqual(project["id"], task["project_id"])
        self.assertEqual(project["id"], summary["project_id"])
        self.assertEqual(1, result["heartbeat_count"])
        self.assertEqual(1, result["processed_count"])

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
        llm_script = Path(self.temp_dir.name) / "fake_supervise_noop_heartbeat.sh"
        llm_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"summary\":\"No new work\",\"priority_focus\":\"steady\",\"issue_creation_needed\":false,\"proposed_tasks\":[]}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        llm_script.chmod(0o755)
        self.env["ACCRUVIA_LLM_BACKEND"] = "command"
        self.env["ACCRUVIA_LLM_COMMAND"] = str(llm_script)

        project = self.run_cli(
            "create-project",
            "supervisor",
            "supervisor project",
            "--no-bootstrap-heartbeat",
        )["project"]
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

        result = self.run_cli("supervise", "--project-id", project["id"], "--worker-id", "supervisor-a", "--one-shot")
        summary = self.run_cli("summary", "--project-id", project["id"])

        self.assertEqual(2, result["processed_count"])
        self.assertEqual([high["id"], low["id"]], result["processed_task_ids"])
        self.assertEqual("idle", result["exit_reason"])
        self.assertEqual(2, summary["metrics"]["tasks_by_status"]["completed"])

    def test_create_task_accepts_explicit_scope_metadata(self) -> None:
        project = self.run_cli("create-project", "scoped", "scoped project")["project"]
        task = self.run_cli(
            "create-task",
            project["id"],
            "Scoped",
            "Scoped objective",
            "--allowed-path",
            "src/routellect/protocols.py",
            "--allowed-path",
            "tests/test_boundary.py",
            "--forbidden-path",
            "README.md",
        )["task"]

        self.assertEqual(
            ["src/routellect/protocols.py", "tests/test_boundary.py"],
            task["scope"]["allowed_paths"],
        )
        self.assertEqual(["README.md"], task["scope"]["forbidden_paths"])

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
