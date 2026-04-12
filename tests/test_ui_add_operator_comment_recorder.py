from __future__ import annotations

import json
import tempfile
import unittest
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from accruvia_harness.config import HarnessConfig
from accruvia_harness.context_recorder import ContextRecorder
from accruvia_harness.domain import (
    MermaidArtifact,
    MermaidStatus,
    Objective,
    ObjectiveStatus,
    Project,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    new_id,
)
from accruvia_harness.interrogation import HarnessQueryService
from accruvia_harness.llm import LLMExecutionResult
from accruvia_harness.store import SQLiteHarnessStore
from accruvia_harness.ui import HarnessUIDataService


class FakeLLMRouter:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.executors = {"fake": object()}

    def execute(self, invocation, telemetry=None):
        invocation.run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = invocation.run_dir / "llm_prompt.txt"
        response_path = invocation.run_dir / "llm_response.md"
        prompt_path.write_text(invocation.prompt, encoding="utf-8")
        response_path.write_text(self.response_text, encoding="utf-8")
        return (
            LLMExecutionResult(
                backend="fake-ui-llm",
                response_text=self.response_text,
                prompt_path=prompt_path,
                response_path=response_path,
                diagnostics={},
            ),
            "fake-ui-llm",
        )


class FakePromotionEngine:
    def __init__(self, store: SQLiteHarnessStore) -> None:
        self.store = store
        self.worker = SimpleNamespace(set_stop_requested=lambda *_a, **_k: None)
        self.repository_promotions = SimpleNamespace(apply_objective=lambda *_a, **_k: None)


class TestAddOperatorCommentRecorder(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.db_path = base / "harness.db"
        self.workspace_root = base / "workspace"
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteHarnessStore(self.db_path)
        self.store.initialize()
        self.project = Project(id=new_id("project"), name="recorder-test", description="Test project")
        self.store.create_project(self.project)
        self.objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Test objective",
            summary="For recorder routing test",
        )
        self.store.create_objective(self.objective)
        self.store.create_mermaid_artifact(
            MermaidArtifact(
                id=new_id("diagram"),
                objective_id=self.objective.id,
                diagram_type="workflow_control",
                version=1,
                status=MermaidStatus.FINISHED,
                summary="Accepted workflow",
                content="flowchart TD\nA[Intent]-->B[Plan]",
                required_for_execution=True,
            )
        )
        self.query_service = HarnessQueryService(self.store)
        self.ctx = SimpleNamespace(
            store=self.store,
            query_service=self.query_service,
            is_test=True,
            config=HarnessConfig.from_payload(
                {
                    "db_path": str(self.db_path),
                    "workspace_root": str(self.workspace_root),
                    "log_path": str(base / "harness.log"),
                    "telemetry_dir": str(base / "telemetry"),
                    "default_project_name": "recorder-test",
                    "default_repo": "",
                    "runtime_backend": "inline",
                    "temporal_target": "",
                    "temporal_namespace": "",
                    "temporal_task_queue": "",
                    "worker_backend": "process",
                    "worker_command": None,
                    "llm_backend": "codex",

                    "llm_command": None,
                    "llm_codex_command": None,
                    "llm_claude_command": None,
                    "llm_accruvia_client_command": None,
                }
            ),
        )
        self.ctx.engine = FakePromotionEngine(self.store)
        self.service = HarnessUIDataService(self.ctx)

    def test_comment_persisted_via_recorder(self) -> None:
        result = self.service.add_operator_comment(
            self.project.id,
            "This is a recorder-routed comment",
            "tester",
            self.objective.id,
        )

        self.assertEqual("tester", result["comment"]["author"])
        self.assertEqual("This is a recorder-routed comment", result["comment"]["text"])
        records = self.store.list_context_records(
            project_id=self.project.id,
            objective_id=self.objective.id,
            record_type="operator_comment",
        )
        self.assertEqual(1, len(records))
        self.assertEqual(result["comment"]["id"], records[0].id)
        self.assertEqual("operator", records[0].author_type)
        self.assertEqual("tester", records[0].author_id)

    def test_service_has_context_recorder_instance(self) -> None:
        self.assertIsInstance(self.service.context_recorder, ContextRecorder)
        self.assertIs(self.service.context_recorder.store, self.store)

    def test_responder_returns_packet_backed_answer(self) -> None:
        fake_router = FakeLLMRouter('{"reply": "The next step is to review your mermaid diagram.", "recommended_action": "review_mermaid", "evidence_refs": [], "mode_shift": "none"}')
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)

        result = self.service.add_operator_comment(
            self.project.id,
            "What should I do next?",
            "tester",
            self.objective.id,
        )

        self.assertTrue(result["reply"]["text"])
        self.assertIn("mermaid", result["reply"]["text"].lower())
        reply_records = self.store.list_context_records(
            project_id=self.project.id,
            objective_id=self.objective.id,
            record_type="harness_reply",
        )
        self.assertEqual(1, len(reply_records))
        self.assertEqual(result["reply"]["id"], reply_records[0].id)

    def test_frustration_still_detected_after_recorder_refactor(self) -> None:
        result = self.service.add_operator_comment(
            self.project.id,
            "I am frustrated and stuck with this UI.",
            "tester",
            self.objective.id,
        )

        self.assertTrue(result["frustration_detected"])
        comment_records = self.store.list_context_records(
            project_id=self.project.id,
            objective_id=self.objective.id,
            record_type="operator_comment",
        )
        self.assertEqual(1, len(comment_records))

    def test_comment_without_objective_uses_recorder(self) -> None:
        result = self.service.add_operator_comment(
            self.project.id,
            "Project-level comment",
            "tester",
        )

        self.assertEqual("Project-level comment", result["comment"]["text"])
        records = self.store.list_context_records(
            project_id=self.project.id,
            record_type="operator_comment",
        )
        self.assertEqual(1, len(records))
        self.assertIsNone(records[0].objective_id)


if __name__ == "__main__":
    unittest.main()
