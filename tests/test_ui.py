from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import (
    Artifact,
    Event,
    MermaidArtifact,
    MermaidStatus,
    Objective,
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
        self.last_prompt = ""
        self.prompts: list[str] = []

    def execute(self, invocation, telemetry=None):
        self.last_prompt = invocation.prompt
        self.prompts.append(invocation.prompt)
        invocation.run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = invocation.run_dir / "llm_prompt.txt"
        response_path = invocation.run_dir / "llm_response.md"
        if "Return JSON only with keys: summary, content." in invocation.prompt:
            response_text = json.dumps(
                {
                    "summary": "Move red-team to start during intake before draft planning.",
                    "content": "flowchart TD\nA[Objective Intake]-->B[Red-Team Intake]\nB-->C[Draft Planning Elements]\nC-->D[Mermaid Draft]",
                }
            )
        else:
            response_text = self.response_text
        prompt_path.write_text(invocation.prompt, encoding="utf-8")
        response_path.write_text(response_text, encoding="utf-8")
        return (
            LLMExecutionResult(
                backend="fake-ui-llm",
                response_text=response_text,
                prompt_path=prompt_path,
                response_path=response_path,
                diagnostics={},
            ),
            "fake-ui-llm",
        )


class HarnessUIDataServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.db_path = base / "harness.db"
        self.workspace_root = base / "workspace"
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteHarnessStore(self.db_path)
        self.store.initialize()
        self.project = Project(id=new_id("project"), name="demo", description="Demo project")
        self.store.create_project(self.project)
        self.parent_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            title="Parent task",
            objective="Top level",
            status=TaskStatus.COMPLETED,
        )
        self.child_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            title="Child task",
            objective="Follow-on",
            parent_task_id=self.parent_task.id,
            strategy="atomicity_split",
            status=TaskStatus.ACTIVE,
        )
        self.store.create_task(self.parent_task)
        self.store.create_task(self.child_task)
        self.objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Clarify operator workflow",
            summary="Need a cleaner path from frustration to investigation",
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
        self.parent_run = Run(
            id=new_id("run"),
            task_id=self.parent_task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary="Completed run",
        )
        self.child_run = Run(
            id=new_id("run"),
            task_id=self.child_task.id,
            status=RunStatus.WORKING,
            attempt=2,
            summary="In progress",
        )
        self.store.create_run(self.parent_run)
        self.store.create_run(self.child_run)
        self.child_run_dir = self.workspace_root / "runs" / self.child_run.id
        self.child_run_dir.mkdir(parents=True, exist_ok=True)
        (self.child_run_dir / "plan.txt").write_text("child plan", encoding="utf-8")
        (self.child_run_dir / "codex_worker.stderr.txt").write_text("stderr trace", encoding="utf-8")
        report_path = self.child_run_dir / "report.json"
        report_path.write_text(json.dumps({"worker_outcome": "working"}), encoding="utf-8")
        self.store.create_artifact(
            Artifact(
                id=new_id("artifact"),
                run_id=self.child_run.id,
                kind="report",
                path=str(report_path),
                summary="Structured report",
            )
        )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="project",
                entity_id=self.project.id,
                event_type="operator_nudge",
                payload={"author": "nudge-user", "note": "Different stream"},
            )
        )
        self.query_service = HarnessQueryService(self.store)
        self.ctx = SimpleNamespace(
            store=self.store,
            query_service=self.query_service,
            config=HarnessConfig.from_payload(
                {
                    "db_path": str(self.db_path),
                    "workspace_root": str(self.workspace_root),
                    "log_path": str(base / "harness.log"),
                    "telemetry_dir": str(base / "telemetry"),
                    "default_project_name": "demo",
                    "default_repo": "",
                    "runtime_backend": "inline",
                    "temporal_target": "",
                    "temporal_namespace": "",
                    "temporal_task_queue": "",
                    "worker_backend": "process",
                    "worker_command": None,
                    "llm_backend": "codex",
                    "llm_model": None,
                    "llm_command": None,
                    "llm_codex_command": None,
                    "llm_claude_command": None,
                    "llm_accruvia_client_command": None,
                }
            ),
        )
        self.service = HarnessUIDataService(self.ctx)

    def test_project_workspace_renders_mermaid_and_hides_nudges_from_comments(self) -> None:
        payload = self.service.project_workspace(self.project.id)

        self.assertEqual(self.project.id, payload["project"]["id"])
        self.assertEqual(self.objective.id, payload["objectives"][0]["id"])
        self.assertFalse(payload["objectives"][0]["execution_gate"]["ready"])
        self.assertEqual([], payload["comments"])
        diagram = payload["diagram"]["mermaid"]
        self.assertIn("Project: demo", diagram)
        self.assertIn("Parent task", diagram)
        self.assertIn("Child task", diagram)
        self.assertIn("-->", diagram)

    def test_new_objective_starts_with_guided_next_step_data(self) -> None:
        created = self.service.create_objective(self.project.id, "Harness UI", "Build the local control surface")
        objective_id = created["objective"]["id"]

        payload = self.service.project_workspace(self.project.id)
        created_objective = next(item for item in payload["objectives"] if item["id"] == objective_id)
        checks = {item["key"]: item for item in created_objective["execution_gate"]["checks"]}

        self.assertFalse(created_objective["execution_gate"]["ready"])
        self.assertFalse(checks["intent_model"]["ok"])
        self.assertFalse(checks["interrogation_complete"]["ok"])
        self.assertTrue(checks["required_mermaid"]["ok"])
        self.assertFalse(checks["mermaid_finished"]["ok"])
        self.assertIn("questions", created_objective["interrogation_review"])

    def test_run_cli_output_reads_artifacts_and_known_run_files(self) -> None:
        payload = self.service.run_cli_output(self.child_run.id)

        labels = [section["label"] for section in payload["sections"]]
        self.assertIn("report", labels)
        self.assertIn("plan", labels)
        self.assertIn("codex worker stderr", labels)
        self.assertIn("headline", payload["summary"])
        self.assertIn("recommended_next", payload["summary"])

    def test_add_operator_comment_creates_separate_comment_stream(self) -> None:
        result = self.service.add_operator_comment(
            self.project.id,
            "Investigate control flow",
            "shaun",
            self.objective.id,
        )

        self.assertEqual("shaun", result["comment"]["author"])
        self.assertTrue(result["reply"]["text"])
        payload = self.service.project_workspace(self.project.id)
        self.assertEqual(1, len(payload["comments"]))
        self.assertEqual("Investigate control flow", payload["comments"][0]["text"])
        self.assertEqual(self.objective.id, payload["comments"][0]["objective_id"])
        self.assertEqual(1, len(payload["replies"]))
        self.assertEqual(self.objective.id, payload["replies"][0]["objective_id"])
        self.assertFalse(result["frustration_detected"])

    def test_add_operator_comment_can_infer_frustration(self) -> None:
        result = self.service.add_operator_comment(
            self.project.id,
            "This UI is confusing and I am stuck.",
            "shaun",
            self.objective.id,
        )

        self.assertTrue(result["frustration_detected"])
        payload = self.service.project_workspace(self.project.id)
        self.assertEqual(1, len(payload["frustrations"]))
        self.assertEqual("This UI is confusing and I am stuck.", payload["frustrations"][0]["text"])
        self.assertIn("frustrated", result["reply"]["text"].lower())

    def test_add_operator_comment_returns_plain_language_next_step_answer(self) -> None:
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Operator wants a reliable workflow",
            success_definition="The flow is understandable",
            non_negotiables=["No hidden steps"],
            frustration_signals=["Stalls"],
        )
        result = self.service.add_operator_comment(
            self.project.id,
            "What am I supposed to do next?",
            "shaun",
            self.objective.id,
        )

        self.assertIn("clarification", result["reply"]["text"].lower())
        self.assertIn("mermaid review", result["reply"]["text"].lower())

    def test_add_operator_comment_logs_memory_retrieval_and_reply_metadata(self) -> None:
        self.service.add_operator_comment(
            self.project.id,
            "Please keep the UI plain language and operator-visible.",
            "shaun",
            self.objective.id,
        )

        result = self.service.add_operator_comment(
            self.project.id,
            "plain language please",
            "shaun",
            self.objective.id,
        )

        self.assertTrue(result["reply"]["retrieved_memories"])
        self.assertIn("plain language", result["reply"]["retrieved_memories"][0]["summary"].lower())
        retrieval_records = self.store.list_context_records(
            project_id=self.project.id,
            objective_id=self.objective.id,
            record_type="ui_memory_retrieval",
        )
        self.assertEqual(2, len(retrieval_records))
        self.assertGreaterEqual(retrieval_records[-1].metadata["retrieved_count"], 1)
        self.assertTrue(retrieval_records[-1].metadata["retrieved_memories"])

    def test_add_operator_comment_uses_llm_router_with_broad_context_when_available(self) -> None:
        fake_router = FakeLLMRouter(
            json.dumps(
                {
                    "reply": "Yes. Red-team should happen before Mermaid review, immediately after draft planning elements are produced.",
                    "recommended_action": "review_mermaid",
                    "evidence_refs": ["interrogation_review", "mermaid"],
                    "mode_shift": "none",
                }
            )
        )
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Move red-team earlier in planning",
            success_definition="The harness interrogates and red-teams planning before Mermaid review",
            non_negotiables=["Red-team all planning outputs"],
            frustration_signals=["Weak planning answers"],
        )

        result = self.service.add_operator_comment(
            self.project.id,
            "Should we consider the red team earlier in the process? If so, where?",
            "shaun",
            self.objective.id,
        )

        self.assertIn("before mermaid review", result["reply"]["text"].lower())
        self.assertEqual("fake-ui-llm", result["reply"]["llm_backend"])
        self.assertIn("all_context_records", fake_router.last_prompt)
        self.assertIn("Should we consider the red team earlier in the process? If so, where?", fake_router.last_prompt)
        self.assertIn("Move red-team earlier in planning", fake_router.last_prompt)

    def test_mermaid_update_request_creates_proposal_and_workspace_surfaces_it(self) -> None:
        fake_router = FakeLLMRouter(
            json.dumps(
                {
                    "reply": "I will propose a Mermaid update for review.",
                    "recommended_action": "review_mermaid",
                    "evidence_refs": ["mermaid"],
                    "mode_shift": "none",
                }
            )
        )
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Move red-team earlier in planning",
            success_definition="The flow shows red-team during intake before planning proceeds",
            non_negotiables=["Red-team before Mermaid review"],
            frustration_signals=["Planning drift"],
        )

        result = self.service.add_operator_comment(
            self.project.id,
            "Update the mermaid to show red-team starting during intake before draft planning elements.",
            "shaun",
            self.objective.id,
        )

        self.assertIsNotNone(result["mermaid_proposal"])
        self.assertIn("proposed mermaid update", result["reply"]["text"].lower())
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        self.assertIsNotNone(objective_payload["diagram_proposal"])
        self.assertIn("Red-Team Intake", objective_payload["diagram_proposal"]["content"])
        self.assertTrue(payload["action_receipts"])
        self.assertIn("proposal generated", payload["action_receipts"][-1]["text"].lower())

    def test_short_mermaid_follow_up_uses_recent_context(self) -> None:
        fake_router = FakeLLMRouter(
            json.dumps(
                {
                    "reply": "I will propose a Mermaid update for review.",
                    "recommended_action": "review_mermaid",
                    "evidence_refs": ["mermaid"],
                    "mode_shift": "none",
                }
            )
        )
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Move red-team earlier in planning",
            success_definition="The flow shows red-team during intake before planning proceeds",
            non_negotiables=["Red-team before Mermaid review"],
            frustration_signals=["Planning drift"],
        )
        self.service.add_operator_comment(
            self.project.id,
            "Update the mermaid to show red-team starting during intake before draft planning elements.",
            "shaun",
            self.objective.id,
        )

        result = self.service.add_operator_comment(
            self.project.id,
            "Do it.",
            "shaun",
            self.objective.id,
        )

        self.assertIsNotNone(result["mermaid_proposal"])

    def test_mermaid_review_structural_edit_request_without_word_mermaid_creates_proposal(self) -> None:
        fake_router = FakeLLMRouter(
            json.dumps(
                {
                    "reply": "I will propose a Mermaid update for review.",
                    "recommended_action": "review_mermaid",
                    "evidence_refs": ["mermaid"],
                    "mode_shift": "none",
                }
            )
        )
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Move red-team earlier in planning",
            success_definition="The flow shows red-team during intake before planning proceeds",
            non_negotiables=["Red-team before Mermaid review"],
            frustration_signals=["Planning drift"],
        )
        self.service.complete_interrogation_review(self.objective.id)
        self.service.update_mermaid_artifact(
            self.objective.id,
            status="paused",
            summary="Diagram under review",
            blocking_reason="Still revising the process flow.",
        )
        self.service.add_operator_comment(
            self.project.id,
            "Update the mermaid to show red-team starting during intake before draft planning elements.",
            "shaun",
            self.objective.id,
        )

        result = self.service.add_operator_comment(
            self.project.id,
            "Add a step after Draft Planning Elements that shows a WIP plan before interrogation.",
            "shaun",
            self.objective.id,
        )

        self.assertIsNotNone(result["mermaid_proposal"])

    def test_reject_mermaid_proposal_can_record_hard_rewind(self) -> None:
        fake_router = FakeLLMRouter(
            json.dumps(
                {
                    "reply": "I will propose a Mermaid update for review.",
                    "recommended_action": "review_mermaid",
                    "evidence_refs": ["mermaid"],
                    "mode_shift": "none",
                }
            )
        )
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Move red-team earlier in planning",
            success_definition="The flow shows red-team during intake before planning proceeds",
            non_negotiables=["Red-team before Mermaid review"],
            frustration_signals=["Planning drift"],
        )
        result = self.service.add_operator_comment(
            self.project.id,
            "Update the mermaid to show red-team starting during intake before draft planning elements.",
            "shaun",
            self.objective.id,
        )

        proposal = result["mermaid_proposal"]
        assert proposal is not None
        rejected = self.service.reject_mermaid_proposal(
            self.objective.id,
            str(proposal["id"]),
            resolution="rewind_hard",
        )

        self.assertEqual("rewind_hard", rejected["resolution"])
        records = self.store.list_context_records(objective_id=self.objective.id, record_type="mermaid_update_rewound")
        self.assertEqual(1, len(records))
        self.assertEqual("rewind_hard", records[0].metadata["resolution"])
        receipts = self.store.list_context_records(objective_id=self.objective.id, record_type="action_receipt")
        self.assertIn("rewound hard", receipts[-1].content.lower())
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        self.assertIsNone(objective_payload["diagram_proposal"])

    def test_accept_mermaid_proposal_finishes_review_and_clears_pending_state(self) -> None:
        fake_router = FakeLLMRouter(
            json.dumps(
                {
                    "reply": "I will propose a Mermaid update for review.",
                    "recommended_action": "review_mermaid",
                    "evidence_refs": ["mermaid"],
                    "mode_shift": "none",
                }
            )
        )
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Move red-team earlier in planning",
            success_definition="The flow shows red-team during intake before planning proceeds",
            non_negotiables=["Red-team before Mermaid review"],
            frustration_signals=["Planning drift"],
        )
        self.service.complete_interrogation_review(self.objective.id)
        self.service.update_mermaid_artifact(
            self.objective.id,
            status="paused",
            summary="Diagram under review",
            blocking_reason="Still revising the process flow.",
        )
        result = self.service.add_operator_comment(
            self.project.id,
            "Update the mermaid to show red-team starting during intake before draft planning elements.",
            "shaun",
            self.objective.id,
        )

        proposal = result["mermaid_proposal"]
        assert proposal is not None
        accepted = self.service.accept_mermaid_proposal(self.objective.id, str(proposal["id"]), async_generation=False)

        self.assertEqual("finished", accepted["diagram"]["status"])
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        self.assertIsNone(objective_payload["diagram_proposal"])
        self.assertEqual("finished", objective_payload["diagram"]["status"])
        checks = {item["key"]: item for item in objective_payload["execution_gate"]["checks"]}
        self.assertTrue(checks["mermaid_finished"]["ok"])

    def test_queue_atomic_generation_derives_units_for_latest_finished_mermaid(self) -> None:
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Operator wants the accepted flow split into atomic units",
            success_definition="Atomic work appears for the accepted Mermaid",
            non_negotiables=["Atomic units must map to the Mermaid"],
            frustration_signals=["No decomposition"],
        )
        self.service.complete_interrogation_review(self.objective.id)
        self.service.update_mermaid_artifact(
            self.objective.id,
            status="finished",
            summary="Accepted control flow",
            blocking_reason="",
            async_generation=False,
        )

        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)

        self.assertEqual("completed", objective_payload["atomic_generation"]["status"])
        self.assertEqual("complete", objective_payload["atomic_generation"]["phase"])
        self.assertTrue(objective_payload["atomic_generation"]["last_activity_at"])
        self.assertGreaterEqual(len(objective_payload["atomic_units"]), 1)
        self.assertTrue(all(unit["title"] for unit in objective_payload["atomic_units"]))

    def test_latest_resolved_proposal_does_not_fall_back_to_older_unresolved_proposal(self) -> None:
        fake_router = FakeLLMRouter(
            json.dumps(
                {
                    "reply": "I will propose a Mermaid update for review.",
                    "recommended_action": "review_mermaid",
                    "evidence_refs": ["mermaid"],
                    "mode_shift": "none",
                }
            )
        )
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)

        first = self.service.add_operator_comment(
            self.project.id,
            "Update the mermaid to move red-team earlier.",
            "shaun",
            self.objective.id,
        )
        self.assertIsNotNone(first["mermaid_proposal"])

        second = self.service.add_operator_comment(
            self.project.id,
            "Update the mermaid to end on operator approval.",
            "shaun",
            self.objective.id,
        )
        proposal = second["mermaid_proposal"]
        assert proposal is not None
        self.service.accept_mermaid_proposal(self.objective.id, str(proposal["id"]), async_generation=False)

        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        self.assertIsNone(objective_payload["diagram_proposal"])

    def test_run_cli_command_wraps_real_harness_cli(self) -> None:
        result = self.service.run_cli_command(f"summary --project-id {self.project.id}")

        self.assertEqual(0, result["exit_code"])
        self.assertIn(self.project.id, result["output"])

    def test_follow_up_how_uses_latest_reply_and_run_context(self) -> None:
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Operator wants a reliable workflow",
            success_definition="The flow is understandable",
            non_negotiables=["No hidden steps"],
            frustration_signals=["Stalls"],
        )
        self.service.update_mermaid_artifact(
            self.objective.id,
            status="finished",
            summary="Workflow accepted",
            blocking_reason="",
            async_generation=False,
        )
        self.service.complete_interrogation_review(self.objective.id)
        linked_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="First slice",
            objective="Review run flow",
            status=TaskStatus.ACTIVE,
            strategy="operator_ergonomics",
        )
        self.store.create_task(linked_task)
        linked_run = Run(
            id=new_id("run"),
            task_id=linked_task.id,
            status=RunStatus.ANALYZING,
            attempt=1,
            summary="Plan and report exist.",
        )
        self.store.create_run(linked_run)
        run_dir = self.workspace_root / "runs" / linked_run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "plan.txt").write_text("plan body", encoding="utf-8")
        (run_dir / "report.json").write_text(json.dumps({"ok": True}), encoding="utf-8")

        self.service.add_operator_comment(
            self.project.id,
            "what's next?",
            "shaun",
            self.objective.id,
        )
        result = self.service.add_operator_comment(
            self.project.id,
            "how?",
            "shaun",
            self.objective.id,
        )

        self.assertIn("review latest run output", result["reply"]["text"].lower())
        self.assertIn("attempt 1", result["reply"]["text"].lower())

    def test_confused_run_review_question_points_to_visible_button(self) -> None:
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Operator wants a reliable workflow",
            success_definition="The flow is understandable",
            non_negotiables=["No hidden steps"],
            frustration_signals=["Stalls"],
        )
        self.service.update_mermaid_artifact(
            self.objective.id,
            status="finished",
            summary="Workflow accepted",
            blocking_reason="",
            async_generation=False,
        )
        self.service.complete_interrogation_review(self.objective.id)
        linked_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="First slice",
            objective="Review run flow",
            status=TaskStatus.ACTIVE,
            strategy="operator_ergonomics",
        )
        self.store.create_task(linked_task)
        linked_run = Run(
            id=new_id("run"),
            task_id=linked_task.id,
            status=RunStatus.FAILED,
            attempt=4,
            summary="Worker failed after report creation.",
        )
        self.store.create_run(linked_run)
        run_dir = self.workspace_root / "runs" / linked_run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.json").write_text(json.dumps({"ok": False}), encoding="utf-8")

        result = self.service.add_operator_comment(
            self.project.id,
            "How do I review the latest run? I don't get it. Where do I look?",
            "shaun",
            self.objective.id,
        )

        self.assertIn("review latest run output", result["reply"]["text"].lower())
        self.assertIn("just below this input box", result["reply"]["text"].lower())

    def test_add_operator_frustration_records_triage_and_updates_objective_status(self) -> None:
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Operator wants a reliable workflow",
            success_definition="The flow is understandable",
            non_negotiables=["No hidden steps"],
            frustration_signals=["Stalls"],
        )
        result = self.service.add_operator_frustration(
            self.project.id,
            "This still feels stuck and unclear.",
            "shaun",
            self.objective.id,
        )

        self.assertEqual("shaun", result["frustration"]["author"])
        self.assertIn("likely_causes", result["frustration"]["triage"])
        payload = self.service.project_workspace(self.project.id)
        self.assertEqual(1, len(payload["frustrations"]))
        self.assertEqual("This still feels stuck and unclear.", payload["frustrations"][0]["text"])
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        self.assertEqual("investigating", objective_payload["status"])

    def test_create_objective_and_intent_model(self) -> None:
        created = self.service.create_objective(self.project.id, "Improve planning", "Separate intent and plan")
        objective_id = created["objective"]["id"]
        seeded_mermaid = self.store.latest_mermaid_artifact(objective_id)
        updated = self.service.update_intent_model(
            objective_id,
            intent_summary="Make planning explicit",
            success_definition="The operator can approve intent before coding",
            non_negotiables=["No code before intent"],
            frustration_signals=["Repeated confusion"],
        )

        self.assertIsNotNone(seeded_mermaid)
        assert seeded_mermaid is not None
        self.assertEqual(MermaidStatus.DRAFT, seeded_mermaid.status)
        self.assertTrue(seeded_mermaid.required_for_execution)
        self.assertEqual("Make planning explicit", updated["intent_model"]["intent_summary"])
        payload = self.service.project_workspace(self.project.id)
        created_objective = next(item for item in payload["objectives"] if item["id"] == objective_id)
        self.assertEqual("Make planning explicit", created_objective["intent_model"]["intent_summary"])
        self.assertFalse(created_objective["interrogation_review"]["completed"])

    def test_update_mermaid_artifact_creates_new_version_and_unblocks_mermaid_gate(self) -> None:
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Operator wants a stable workflow before code runs",
            success_definition="The workflow is accepted and execution can proceed",
            non_negotiables=["Mermaid must be finished"],
            frustration_signals=["Repeated stops"],
        )
        self.service.complete_interrogation_review(self.objective.id)

        paused = self.service.update_mermaid_artifact(
            self.objective.id,
            status="paused",
            summary="Need to clarify the planning branch",
            blocking_reason="Branch ownership is still ambiguous.",
        )
        finished = self.service.update_mermaid_artifact(
            self.objective.id,
            status="finished",
            summary="Workflow accepted",
            blocking_reason="",
            async_generation=False,
        )

        self.assertEqual("paused", paused["diagram"]["status"])
        self.assertEqual("finished", finished["diagram"]["status"])
        self.assertEqual(3, finished["diagram"]["version"])
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        checks = {item["key"]: item for item in objective_payload["execution_gate"]["checks"]}
        self.assertTrue(checks["objective_exists"]["ok"])
        self.assertTrue(checks["intent_model"]["ok"])
        self.assertTrue(checks["interrogation_complete"]["ok"])
        self.assertTrue(checks["required_mermaid"]["ok"])
        self.assertTrue(checks["mermaid_finished"]["ok"])
        self.assertEqual("completed", objective_payload["atomic_generation"]["status"])
        self.assertGreaterEqual(len(objective_payload["atomic_units"]), 1)

    def test_complete_interrogation_review_marks_objective_ready_for_mermaid(self) -> None:
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Operator wants clearer planning before execution",
            success_definition="The harness asks clarifying questions before Mermaid review",
            non_negotiables=["No silent assumptions"],
            frustration_signals=["Repeated confusion"],
        )

        result = self.service.complete_interrogation_review(self.objective.id)

        self.assertTrue(result["interrogation_review"]["completed"])
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        self.assertTrue(objective_payload["interrogation_review"]["completed"])
        checks = {item["key"]: item for item in objective_payload["execution_gate"]["checks"]}
        self.assertTrue(checks["interrogation_complete"]["ok"])


if __name__ == "__main__":
    unittest.main()
