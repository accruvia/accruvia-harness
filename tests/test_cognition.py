from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.cognition import build_cognition_registry
from accruvia_harness.config import HarnessConfig
from accruvia_harness.engine import HarnessEngine
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
        events = store.list_events("project", project.id)
        self.assertIn("heartbeat_completed", [event.event_type for event in events])
