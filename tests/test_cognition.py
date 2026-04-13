from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.cognition import BrainSource, GenericCognitionAdapter, build_cognition_registry
from accruvia_harness.config import HarnessConfig
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.workers import LocalArtifactWorker
from accruvia_harness.project_adapters import build_project_adapter_registry
from accruvia_harness.store import SQLiteHarnessStore


class CognitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.plugin_root = self.base / "plugins"
        self.plugin_root.mkdir()
        self.repo_root = self.base / "repo"
        self.repo_root.mkdir()
        (self.repo_root / "README.md").write_text("# Demo project\n", encoding="utf-8")
        self.module_name = "private_cognition_plugin"
        (self.plugin_root / f"{self.module_name}.py").write_text(
            "import json\n"
            "import os\n"
            "from pathlib import Path\n\n"
            "class DemoProjectAdapter:\n"
            "    name = 'demo'\n"
            "    def prepare_workspace(self, project, task, run, run_dir):\n"
            "        from accruvia_harness.project_adapters import ProjectWorkspace\n"
            "        root = Path(os.environ['DEMO_REPO_ROOT'])\n"
            "        return ProjectWorkspace(project_root=root, metadata_files=[root / 'README.md'], environment={'DEMO_REPO_ROOT': str(root)}, diagnostics={'project_adapter': self.name})\n\n"
            "class DemoCognitionAdapter:\n"
            "    name = 'demo'\n"
            "    def resolve_project_root(self, project):\n"
            "        return Path(os.environ['DEMO_REPO_ROOT'])\n"
            "    def list_brain_paths(self, project, project_root):\n"
            "        return [project_root / 'README.md']\n"
            "    def build_context(self, project, project_root, project_summary, context_packet, source_documents):\n"
            "        return {'project_name': project.name, 'source_count': len(source_documents), 'project_summary': project_summary, 'context_packet': context_packet}\n"
            "    def build_prompt(self, project, context):\n"
            "        return 'heartbeat for ' + project.name + '\\n' + json.dumps(context, sort_keys=True)\n"
            "    def parse_response(self, response_text):\n"
            "        return json.loads(response_text)\n\n"
            "def register_project_adapters(registry):\n"
            "    registry.register(DemoProjectAdapter())\n\n"
            "def register_cognition_adapters(registry):\n"
            "    registry.register(DemoCognitionAdapter())\n",
            encoding="utf-8",
        )
        sys.path.insert(0, str(self.plugin_root))
        self.addCleanup(lambda: sys.path.remove(str(self.plugin_root)))

    def test_cognition_registry_can_load_external_module(self) -> None:
        registry = build_cognition_registry((self.module_name,))
        self.assertIn("demo", registry.names())

    def test_generic_cognition_adapter_serializes_brain_sources(self) -> None:
        adapter = GenericCognitionAdapter()
        context = adapter.build_context(
            project=type("ProjectStub", (), {"id": "p1", "name": "Demo", "description": "Demo", "adapter_name": "generic"})(),
            project_root=self.repo_root,
            project_summary={"metrics": {}},
            context_packet={"focus_tasks": []},
            source_documents=[BrainSource(path=str(self.repo_root / "README.md"), kind="md", summary="Demo", content="# Demo")],
        )

        self.assertEqual("Demo", context["brain_sources"][0]["summary"])

    def test_generic_cognition_prompt_sets_global_brain_policy(self) -> None:
        adapter = GenericCognitionAdapter()
        project = type("ProjectStub", (), {"id": "p1", "name": "Demo", "description": "Demo", "adapter_name": "generic"})()
        prompt = adapter.build_prompt(project, {"project": {"name": "Demo"}, "telemetry": {"runs": 3}})

        self.assertIn("global project brain", prompt)
        self.assertIn("There is no fixed cap on the number of tasks.", prompt)
        self.assertIn("Reject feature creep", prompt)
        self.assertIn("queue behavior, and strategy overhead", prompt)
        self.assertIn("Do not treat failed, blocked, stale, or crash-looping tasks as adequate backlog coverage", prompt)
        self.assertIn("Healthy idle is not completion", prompt)
        self.assertIn("do not recommend more than 1800 seconds before the next heartbeat", prompt)
        self.assertIn("strict JSON with keys: summary, priority_focus, issue_creation_needed, proposed_tasks, risks", prompt)
        self.assertIn("next_heartbeat_seconds", prompt)

    def test_generic_cognition_adapter_parses_next_heartbeat_seconds(self) -> None:
        adapter = GenericCognitionAdapter()

        parsed = adapter.parse_response(
            json.dumps(
                {
                    "summary": "Healthy project",
                    "priority_focus": "stability",
                    "issue_creation_needed": False,
                    "proposed_tasks": [],
                    "risks": [],
                    "next_heartbeat_seconds": 2700,
                }
            )
        )

        self.assertEqual(2700, parsed["next_heartbeat_seconds"])

    def test_engine_heartbeat_uses_external_cognition_adapter(self) -> None:
        llm_script = self.base / "fake_llm.sh"
        llm_script.write_text(
            "#!/usr/bin/env bash\n"
            "cat > /dev/null < \"$ACCRUVIA_LLM_PROMPT_PATH\"\n"
            "printf '{\"summary\":\"Focus router extraction\",\"priority_focus\":\"extract routing core\",\"issue_creation_needed\":true,\"proposed_tasks\":[{\"title\":\"Extract docs\",\"objective\":\"Split docs cleanly\",\"priority\":120,\"rationale\":\"Keep repo boundary explicit\"}]}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        llm_script.chmod(0o755)

        os.environ["DEMO_REPO_ROOT"] = str(self.repo_root)
        self.addCleanup(lambda: os.environ.pop("DEMO_REPO_ROOT", None))

        store = SQLiteHarnessStore(self.base / "harness.db")
        store.initialize()
        config = HarnessConfig.from_payload(
            {
                **HarnessConfig.from_env(
                    db_path=self.base / "harness.db",
                    workspace_root=self.base / "workspace",
                    log_path=self.base / "harness.log",
                ).to_payload(),
                "project_adapter_modules": (self.module_name,),
                "cognition_modules": (self.module_name,),
                "llm_backend": "command",
                "llm_command": str(llm_script),
            }
        )
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=store,
            workspace_root=config.workspace_root,
            project_adapter_registry=build_project_adapter_registry(config.project_adapter_modules),
            cognition_registry=build_cognition_registry(config.cognition_modules),
        )
        from accruvia_harness.llm import build_llm_router

        engine.set_llm_router(build_llm_router(config))

        project = engine.create_project("Routellect", "Demo project", adapter_name="demo")
        task = engine.create_task_with_policy(
            project_id=project.id,
            title="Seed task",
            objective="Have at least one task in the system",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="generic",
            strategy="default",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.assertIsNotNone(task)

        heartbeat = engine.heartbeat(project.id)

        self.assertEqual("demo", heartbeat.adapter_name)
        self.assertTrue(Path(heartbeat.context_path).exists())
        self.assertTrue(Path(heartbeat.sources_path).exists())
        self.assertTrue(Path(heartbeat.response_path).exists())
        self.assertEqual("extract routing core", heartbeat.analysis["priority_focus"])
        self.assertEqual(1, len(heartbeat.analysis["proposed_tasks"]))
        self.assertEqual(1, len(heartbeat.created_tasks))
        self.assertEqual("Extract docs", heartbeat.created_tasks[0]["title"])
        events = store.list_events("project", project.id)
        self.assertIn("heartbeat_completed", [event.event_type for event in events])
        tasks = store.list_tasks(project.id)
        self.assertEqual(2, len(tasks))

    def test_heartbeat_dedupes_proposed_tasks(self) -> None:
        os.environ["DEMO_REPO_ROOT"] = str(self.repo_root)
        self.addCleanup(lambda: os.environ.pop("DEMO_REPO_ROOT", None))

        store = SQLiteHarnessStore(self.base / "dedupe.db")
        store.initialize()
        llm_script = self.base / "fake_llm_dedupe.sh"
        config = HarnessConfig.from_payload(
            {
                **HarnessConfig.from_env(
                    db_path=self.base / "dedupe.db",
                    workspace_root=self.base / "workspace-dedupe",
                    log_path=self.base / "harness-dedupe.log",
                ).to_payload(),
                "project_adapter_modules": (self.module_name,),
                "cognition_modules": (self.module_name,),
                "llm_backend": "command",
                "llm_command": str(llm_script),
            }
        )
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=store,
            workspace_root=config.workspace_root,
            project_adapter_registry=build_project_adapter_registry(config.project_adapter_modules),
            cognition_registry=build_cognition_registry(config.cognition_modules),
        )
        from accruvia_harness.llm import build_llm_router

        engine.set_llm_router(build_llm_router(config))
        project = engine.create_project("Routellect", "Demo project", adapter_name="demo")
        seed = engine.create_task_with_policy(
            project_id=project.id,
            title="Seed task",
            objective="Broad work",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="generic",
            strategy="default",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        llm_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"summary\":\"Split broad work\",\"priority_focus\":\"split boundary work\",\"issue_creation_needed\":true,"
            f"\\\"proposed_tasks\\\":[{{\\\"title\\\":\\\"Split boundary docs\\\",\\\"objective\\\":\\\"Narrow docs cleanup\\\",\\\"priority\\\":140,\\\"rationale\\\":\\\"Keep issue atomic\\\",\\\"split_of_task_id\\\":\\\"{seed.id}\\\",\\\"allowed_paths\\\":[\\\"README.md\\\"]}}]}}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        llm_script.chmod(0o755)

        first = engine.heartbeat(project.id)
        second = engine.heartbeat(project.id)

        self.assertEqual(1, len(first.created_tasks))
        self.assertEqual(0, len(second.created_tasks))
        self.assertEqual("duplicate_task", second.skipped_tasks[0]["reason"])

    def test_heartbeat_accepts_priority_labels(self) -> None:
        llm_script = self.base / "fake_llm_priority.sh"
        llm_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"summary\":\"Split broad work\",\"priority_focus\":\"split boundary work\",\"issue_creation_needed\":true,"
            "\\\"proposed_tasks\\\":[{\\\"title\\\":\\\"Priority label task\\\",\\\"objective\\\":\\\"Use a labeled priority\\\",\\\"priority\\\":\\\"P0\\\",\\\"rationale\\\":\\\"Keep issue atomic\\\"}]}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        llm_script.chmod(0o755)
        os.environ["DEMO_REPO_ROOT"] = str(self.repo_root)
        self.addCleanup(lambda: os.environ.pop("DEMO_REPO_ROOT", None))

        store = SQLiteHarnessStore(self.base / "priority.db")
        store.initialize()
        config = HarnessConfig.from_payload(
            {
                **HarnessConfig.from_env(
                    db_path=self.base / "priority.db",
                    workspace_root=self.base / "workspace-priority",
                    log_path=self.base / "harness-priority.log",
                ).to_payload(),
                "project_adapter_modules": (self.module_name,),
                "cognition_modules": (self.module_name,),
                "llm_backend": "command",
                "llm_command": str(llm_script),
            }
        )
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=store,
            workspace_root=config.workspace_root,
            project_adapter_registry=build_project_adapter_registry(config.project_adapter_modules),
            cognition_registry=build_cognition_registry(config.cognition_modules),
        )
        from accruvia_harness.llm import build_llm_router

        engine.set_llm_router(build_llm_router(config))
        project = engine.create_project("Routellect", "Demo project", adapter_name="demo")
        heartbeat = engine.heartbeat(project.id)

        self.assertEqual(1, len(heartbeat.created_tasks))
        self.assertEqual(1000, heartbeat.created_tasks[0]["priority"])

    def test_heartbeat_caps_brain_source_payload_size(self) -> None:
        large = self.repo_root / "docs.md"
        large.write_text("x" * 20000, encoding="utf-8")
        os.environ["DEMO_REPO_ROOT"] = str(self.repo_root)
        self.addCleanup(lambda: os.environ.pop("DEMO_REPO_ROOT", None))
        store = SQLiteHarnessStore(self.base / "size.db")
        store.initialize()
        config = HarnessConfig.from_payload(
            {
                **HarnessConfig.from_env(
                    db_path=self.base / "size.db",
                    workspace_root=self.base / "workspace-size",
                    log_path=self.base / "harness-size.log",
                ).to_payload(),
                "project_adapter_modules": (self.module_name,),
                "cognition_modules": (self.module_name,),
            }
        )
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=store,
            workspace_root=config.workspace_root,
            project_adapter_registry=build_project_adapter_registry(config.project_adapter_modules),
            cognition_registry=build_cognition_registry(config.cognition_modules),
        )
        project = engine.create_project("Routellect", "Demo project", adapter_name="demo")
        heartbeat = engine.heartbeat(project.id)

        total_bytes = sum(len(item["content"].encode("utf-8")) for item in heartbeat.sources)
        self.assertLessEqual(total_bytes, 12000)

    def test_heartbeat_uses_dedicated_timeout_override(self) -> None:
        os.environ["DEMO_REPO_ROOT"] = str(self.repo_root)
        self.addCleanup(lambda: os.environ.pop("DEMO_REPO_ROOT", None))

        store = SQLiteHarnessStore(self.base / "heartbeat-timeout.db")
        store.initialize()
        config = HarnessConfig.from_payload(
            {
                **HarnessConfig.from_env(
                    db_path=self.base / "heartbeat-timeout.db",
                    workspace_root=self.base / "workspace-heartbeat-timeout",
                    log_path=self.base / "harness-heartbeat-timeout.log",
                ).to_payload(),
                "project_adapter_modules": (self.module_name,),
                "cognition_modules": (self.module_name,),
                "heartbeat_timeout_seconds": 2400,
            }
        )

        class TimeoutCapturingRouter:
            def __init__(self) -> None:
                self.executors = {"command": object()}
                self.invocations = []

            def execute(self, invocation, telemetry=None):
                from accruvia_harness.llm import LLMExecutionResult

                self.invocations.append(invocation)
                return (
                    LLMExecutionResult(
                        backend="command",
                        response_text='{"summary":"Timeout captured","priority_focus":"heartbeat","issue_creation_needed":false,"proposed_tasks":[]}',
                        prompt_path=invocation.run_dir / "llm_prompt.txt",
                        response_path=invocation.run_dir / "llm_response.md",
                        diagnostics={"timeout_seconds": invocation.timeout_seconds_override},
                    ),
                    "command",
                )

        router = TimeoutCapturingRouter()
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=store,
            workspace_root=config.workspace_root,
            project_adapter_registry=build_project_adapter_registry(config.project_adapter_modules),
            cognition_registry=build_cognition_registry(config.cognition_modules),
            heartbeat_timeout_seconds=config.heartbeat_timeout_seconds,
            llm_router=router,
        )
        project = engine.create_project("Routellect", "Demo project", adapter_name="demo")

        heartbeat = engine.heartbeat(project.id)

        self.assertEqual(2400, router.invocations[0].timeout_seconds_override)
        self.assertEqual("Timeout captured", heartbeat.analysis["summary"])

    def test_heartbeat_falls_back_between_multiple_llm_providers(self) -> None:
        claude_script = self.base / "fake_heartbeat_claude_fail.sh"
        claude_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'claude unavailable\\n' >&2\n"
            "exit 7\n",
            encoding="utf-8",
        )
        claude_script.chmod(0o755)
        codex_script = self.base / "fake_heartbeat_codex_success.sh"
        codex_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"summary\":\"Fallback succeeded\",\"priority_focus\":\"router\",\"issue_creation_needed\":false,"
            "\\\"proposed_tasks\\\":[]}' > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
            encoding="utf-8",
        )
        codex_script.chmod(0o755)

        os.environ["DEMO_REPO_ROOT"] = str(self.repo_root)
        self.addCleanup(lambda: os.environ.pop("DEMO_REPO_ROOT", None))

        store = SQLiteHarnessStore(self.base / "heartbeat-fallback.db")
        store.initialize()
        config = HarnessConfig.from_payload(
            {
                **HarnessConfig.from_env(
                    db_path=self.base / "heartbeat-fallback.db",
                    workspace_root=self.base / "workspace-heartbeat-fallback",
                    log_path=self.base / "harness-heartbeat-fallback.log",
                ).to_payload(),
                "project_adapter_modules": (self.module_name,),
                "cognition_modules": (self.module_name,),
                "llm_backend": "auto",
                "llm_codex_command": str(codex_script),
                "llm_claude_command": str(claude_script),
            }
        )
        engine = HarnessEngine(
            worker=LocalArtifactWorker(),
            store=store,
            workspace_root=config.workspace_root,
            project_adapter_registry=build_project_adapter_registry(config.project_adapter_modules),
            cognition_registry=build_cognition_registry(config.cognition_modules),
            heartbeat_timeout_seconds=config.heartbeat_timeout_seconds,
        )
        from accruvia_harness.llm import build_llm_router

        engine.set_llm_router(build_llm_router(config))
        project = engine.create_project("Routellect", "Demo project", adapter_name="demo")

        heartbeat = engine.heartbeat(project.id)

        self.assertEqual("codex", heartbeat.llm_backend)
        self.assertEqual("Fallback succeeded", heartbeat.analysis["summary"])
