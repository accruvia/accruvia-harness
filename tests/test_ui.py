from __future__ import annotations

import json
import tempfile
import threading
import unittest
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import accruvia_harness.ui as ui_module
from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import (
    Artifact,
    ContextRecord,
    Event,
    MermaidArtifact,
    MermaidStatus,
    Objective,
    ObjectiveStatus,
    Project,
    PromotionRecord,
    PromotionStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    new_id,
)
from accruvia_harness.interrogation import HarnessQueryService
from accruvia_harness.llm import LLMExecutionResult
from accruvia_harness.store import SQLiteHarnessStore
from accruvia_harness.ui import BackgroundSupervisorCoordinator, HarnessUIDataService


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
        elif "Return JSON only with keys: summary, packets." in invocation.prompt:
            response_text = json.dumps(
                {
                    "summary": "Objective review generated.",
                    "packets": [
                        {
                            "reviewer": "QA agent",
                            "dimension": "unit_test_coverage",
                            "verdict": "concern",
                            "progress_status": "improving",
                            "severity": "medium",
                            "owner_scope": "objective review evidence",
                            "summary": "Unit coverage should be inspected before promotion.",
                            "findings": ["Review the completed task reports for test evidence."],
                            "evidence": ["64 completed tasks"],
                            "required_artifact_type": "objective_review_packet",
                            "artifact_schema": {
                                "type": "objective_review_packet",
                                "description": "Persist a QA review packet that cites concrete completed-task test artifacts.",
                                "required_fields": ["review_id", "reviewer", "dimension", "verdict", "artifacts"],
                            },
                            "closure_criteria": "A recorded QA review packet must cite completed-task unit-test evidence and conclude the concern is resolved or pass.",
                            "evidence_required": "An objective review packet referencing concrete completed-task test artifacts.",
                            "repeat_reason": "This concern is improving because later rounds add more test evidence, but the board still wants explicit artifact-backed QA closure.",
                        },
                        {
                            "reviewer": "Structure agent",
                            "dimension": "code_structure",
                            "verdict": "pass",
                            "progress_status": "resolved",
                            "severity": "",
                            "owner_scope": "",
                            "summary": "No structural blocker was identified from the objective summary.",
                            "findings": [],
                            "evidence": ["5 waived historical failures"],
                            "closure_criteria": "",
                            "evidence_required": "",
                            "repeat_reason": "",
                        },
                    ],
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
                diagnostics={
                    "prompt_tokens": 120,
                    "completion_tokens": 80,
                    "total_tokens": 200,
                    "cost_usd": 0.0123,
                    "latency_ms": 987,
                },
            ),
            "fake-ui-llm",
        )


class InvalidObjectiveReviewRouter(FakeLLMRouter):
    def execute(self, invocation, telemetry=None):
        self.last_prompt = invocation.prompt
        self.prompts.append(invocation.prompt)
        invocation.run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = invocation.run_dir / "llm_prompt.txt"
        response_path = invocation.run_dir / "llm_response.md"
        response_text = self.response_text
        prompt_path.write_text(invocation.prompt, encoding="utf-8")
        response_path.write_text(response_text, encoding="utf-8")
        return (
            LLMExecutionResult(
                backend="fake-ui-llm",
                response_text=response_text,
                prompt_path=prompt_path,
                response_path=response_path,
                diagnostics={
                    "prompt_tokens": 120,
                    "completion_tokens": 80,
                    "total_tokens": 200,
                    "cost_usd": 0.0123,
                    "latency_ms": 987,
                },
            ),
            "fake-ui-llm",
        )


class ZeroUsageObjectiveReviewRouter(FakeLLMRouter):
    def execute(self, invocation, telemetry=None):
        self.last_prompt = invocation.prompt
        self.prompts.append(invocation.prompt)
        invocation.run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = invocation.run_dir / "llm_prompt.txt"
        response_path = invocation.run_dir / "llm_response.md"
        response_text = json.dumps(
            {
                "summary": "Objective review generated.",
                "packets": [
                    {
                        "reviewer": "Ops agent",
                        "dimension": "devops",
                        "verdict": "concern",
                        "progress_status": "improving",
                        "severity": "medium",
                        "owner_scope": "telemetry",
                        "summary": "Need one completed telemetry artifact.",
                        "findings": ["No completed review telemetry artifact is visible."],
                        "evidence": ["No terminal review artifact was persisted for the same review_id."],
                        "required_artifact_type": "review_cycle_telemetry",
                        "artifact_schema": {
                            "type": "review_cycle_telemetry",
                            "description": "Persist one completed review-cycle telemetry export with terminal event evidence.",
                            "required_fields": ["review_id", "start_event", "packet_persistence_events", "terminal_event", "linked_outcome"],
                        },
                        "closure_criteria": "Provide one completed objective-review telemetry artifact.",
                        "evidence_required": "A persisted telemetry export for one completed review cycle.",
                        "repeat_reason": "Still waiting on the requested telemetry artifact.",
                    }
                ],
            }
        )
        prompt_path.write_text(invocation.prompt, encoding="utf-8")
        response_path.write_text(response_text, encoding="utf-8")
        return (
            LLMExecutionResult(
                backend="fake-ui-llm",
                response_text=response_text,
                prompt_path=prompt_path,
                response_path=response_path,
                diagnostics={
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "latency_ms": 0.0,
                },
            ),
            "fake-ui-llm",
        )


class HarnessUIDataServiceTests(unittest.TestCase):
    def test_objective_create_view_uses_dedicated_page_form(self) -> None:
        self.assertIn('data-view="objective-create"', ui_module._OBJECTIVE_CREATE_HTML)
        self.assertIn('page-create-objective-form', ui_module._OBJECTIVE_CREATE_HTML)
        self.assertIn('page-create-objective-title', ui_module._OBJECTIVE_CREATE_HTML)
        self.assertNotIn("window.prompt('New objective title:')", ui_module._APP_JS)

    def test_token_performance_view_exists(self) -> None:
        self.assertIn('data-view="token-performance"', ui_module._TOKEN_PERFORMANCE_HTML)
        self.assertIn('token-performance-content', ui_module._TOKEN_PERFORMANCE_HTML)
        self.assertIn('/token-performance', ui_module._APP_JS)

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
            is_test=True,
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
        self.assertTrue(any("proposal generated" in item["text"].lower() for item in payload["action_receipts"]))

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
        self.assertIsInstance(objective_payload["atomic_units"], list)
        self.assertTrue(all(unit["title"] for unit in objective_payload["atomic_units"]))

    def test_atomic_units_use_live_objective_tasks_as_canonical_state(self) -> None:
        self.service.update_intent_model(
            self.objective.id,
            intent_summary="Operator wants accepted Mermaid units and live follow-on work in one view",
            success_definition="Atomic panel reflects current objective task truth",
            non_negotiables=["Do not fork task state between published units and live work"],
            frustration_signals=["Atomic panel is stale"],
        )
        self.service.complete_interrogation_review(self.objective.id)
        self.service.update_mermaid_artifact(
            self.objective.id,
            status="finished",
            summary="Accepted control flow",
            blocking_reason="",
            async_generation=False,
        )
        published_ids = {
            unit["id"]
            for unit in self.service.project_workspace(self.project.id)["objectives"][0]["atomic_units"]
        }
        extra_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Follow-on remediation task",
            objective="Address review feedback",
            status=TaskStatus.ACTIVE,
            strategy="atomic_follow_on",
        )
        self.store.create_task(extra_task)

        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        units_by_id = {unit["id"]: unit for unit in objective_payload["atomic_units"]}

        self.assertIn(extra_task.id, units_by_id)
        self.assertEqual("active", units_by_id[extra_task.id]["status"])
        self.assertFalse(units_by_id[extra_task.id]["published_unit"])
        for task_id in published_ids:
            self.assertTrue(units_by_id[task_id]["published_unit"])

    def test_project_workspace_surfaces_promotion_review_summary_and_packets(self) -> None:
        reviewed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Reviewed task",
            objective="Ship the promotion review panel",
            status=TaskStatus.COMPLETED,
        )
        failed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Historical failed task",
            objective="Old control-plane implementation path",
            status=TaskStatus.FAILED,
            external_ref_metadata={"failed_task_disposition": {"kind": "waive_obsolete"}},
        )
        reviewed_run = Run(
            id=new_id("run"),
            task_id=reviewed_task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary="Complete",
        )
        self.store.create_task(reviewed_task)
        self.store.create_task(failed_task)
        self.store.create_run(reviewed_run)
        self.store.create_promotion(
            PromotionRecord(
                id=new_id("promotion"),
                task_id=reviewed_task.id,
                run_id=reviewed_run.id,
                status=PromotionStatus.APPROVED,
                summary="Promotion approved by the agent review.",
                details={
                    "affirmation": {"backend": "codex", "rationale": "The implementation matches intent."},
                    "validators": [{"validator": "qa", "issues": []}],
                },
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                project_id=self.project.id,
                objective_id=self.objective.id,
                task_id=failed_task.id,
                record_type="failed_task_waived",
                content="Superseded by manual control-plane implementation.",
                metadata={"task_id": failed_task.id, "disposition": "waive_obsolete"},
            )
        )

        self.service.project_workspace(self.project.id)
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        review = objective_payload["promotion_review"]

        self.assertTrue(review["ready"])
        self.assertEqual(1, review["task_counts"]["completed"])
        self.assertEqual(1, review["task_counts"]["failed"])
        self.assertEqual(1, review["waived_failed_count"])
        self.assertEqual(0, review["unresolved_failed_count"])
        self.assertEqual(1, review["review_packet_count"])
        self.assertEqual("Reviewed task", review["review_packets"][0]["task_title"])
        self.assertEqual("codex", review["review_packets"][0]["latest"]["details"]["affirmation"]["backend"])
        self.assertEqual("waived", review["failed_tasks"][0]["effective_status"])

    def test_project_workspace_marks_unresolved_failed_tasks_as_blocking_review(self) -> None:
        failed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Unresolved failed task",
            objective="Still blocking promotion readiness",
            status=TaskStatus.FAILED,
        )
        self.store.create_task(failed_task)

        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        review = objective_payload["promotion_review"]

        self.assertFalse(review["ready"])
        self.assertEqual(1, review["unresolved_failed_count"])
        self.assertIn("Resolve or disposition", review["next_action"])

    def test_project_workspace_recommends_promotion_review_for_resolved_objective(self) -> None:
        completed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Complete promotion-ready task",
            objective="No blockers remain",
            status=TaskStatus.COMPLETED,
        )
        self.store.create_task(completed_task)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.RESOLVED)

        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)

        self.assertEqual("promotion-review", objective_payload["recommended_view"])
        self.assertEqual("promotion_review_pending", objective_payload["promotion_review"]["phase"])

    def test_project_workspace_recommends_atomic_when_objective_returns_to_execution(self) -> None:
        active_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Remediation task",
            objective="Objective moved back into execution",
            status=TaskStatus.ACTIVE,
        )
        self.store.create_task(active_task)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.EXECUTING)

        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)

        self.assertEqual("atomic", objective_payload["recommended_view"])
        self.assertEqual("execution", objective_payload["promotion_review"]["phase"])

    def test_queue_objective_review_generates_objective_level_packets(self) -> None:
        fake_router = FakeLLMRouter("{}")
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        completed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Review-ready task",
            objective="Execution is complete",
            status=TaskStatus.COMPLETED,
        )
        self.store.create_task(completed_task)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.RESOLVED)

        result = self.service.queue_objective_review(self.objective.id, async_mode=False)
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        review = objective_payload["promotion_review"]

        self.assertEqual("completed", result["objective_review_state"]["status"])
        self.assertEqual("execution", review["phase"])
        self.assertEqual("atomic", objective_payload["recommended_view"])
        self.assertEqual(2, review["review_packet_count"])
        self.assertEqual(2, review["objective_review_packet_count"])
        self.assertEqual("objective_review", review["review_packets"][0]["source"])
        self.assertIn(review["review_packets"][0]["dimension"], {"unit_test_coverage", "code_structure"})
        self.assertEqual(200, review["review_packets"][0]["llm_usage"]["total_tokens"])
        self.assertAlmostEqual(0.0123, review["review_packets"][0]["llm_usage"]["cost_usd"])
        self.assertEqual(1, len(review["review_rounds"]))
        self.assertEqual(1, review["review_rounds"][0]["round_number"])
        self.assertEqual("remediating", review["review_rounds"][0]["status"])
        qa_packet = next(packet for packet in review["review_rounds"][0]["packets"] if packet["dimension"] == "unit_test_coverage")
        self.assertEqual("objective_review_packet", qa_packet["required_artifact_type"])
        self.assertEqual("objective_review_packet", qa_packet["evidence_contract"]["required_artifact_type"])
        self.assertTrue(review["review_rounds"][0]["review_cycle_artifact"]["record_id"])
        remediation_tasks = [
            task for task in payload["tasks"]
            if task["objective_id"] == self.objective.id and task["strategy"] == "objective_review_remediation"
        ]
        self.assertEqual(1, len(remediation_tasks))
        self.assertIn("Produce the required review evidence artifact `objective_review_packet`", remediation_tasks[0]["objective"])

    def test_queue_objective_review_marks_usage_unreported_when_backend_returns_zero_usage(self) -> None:
        fake_router = ZeroUsageObjectiveReviewRouter("{}")
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        completed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Review-ready task",
            objective="Execution is complete",
            status=TaskStatus.COMPLETED,
        )
        self.store.create_task(completed_task)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.RESOLVED)

        self.service.queue_objective_review(self.objective.id, async_mode=False)
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        packet = objective_payload["promotion_review"]["review_rounds"][0]["packets"][0]
        self.assertFalse(packet["llm_usage_reported"])
        self.assertEqual("unreported", packet["llm_usage_source"])

    def test_project_workspace_infers_historical_zero_usage_packets_are_unreported(self) -> None:
        review_id = new_id("objective_review")
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_review_started",
                project_id=self.project.id,
                objective_id=self.objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Started review.",
                metadata={"review_id": review_id},
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_review_packet",
                project_id=self.project.id,
                objective_id=self.objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Historical packet.",
                metadata={
                    "review_id": review_id,
                    "reviewer": "Ops agent",
                    "dimension": "devops",
                    "verdict": "concern",
                    "progress_status": "improving",
                    "severity": "medium",
                    "owner_scope": "telemetry",
                    "findings": ["Need one completed telemetry artifact."],
                    "evidence": ["Packet persistence exists but usage was not reported."],
                    "required_artifact_type": "review_cycle_telemetry",
                    "artifact_schema": {
                        "type": "review_cycle_telemetry",
                        "description": "Persist one completed review-cycle telemetry export with terminal event evidence.",
                        "required_fields": ["review_id", "start_event", "packet_persistence_events", "terminal_event", "linked_outcome"],
                    },
                    "closure_criteria": "Persist one completed objective-review telemetry artifact.",
                    "evidence_required": "A telemetry export for one completed review cycle.",
                    "repeat_reason": "Still waiting on the requested telemetry artifact.",
                    "llm_usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "cost_usd": 0.0,
                        "latency_ms": 0.0,
                        "shared_invocation": True,
                    },
                },
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_review_completed",
                project_id=self.project.id,
                objective_id=self.objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Completed review.",
                metadata={"review_id": review_id, "packet_count": 1},
            )
        )

        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        packet = objective_payload["promotion_review"]["review_rounds"][0]["packets"][0]
        self.assertFalse(packet["llm_usage_reported"])
        self.assertEqual("unreported", packet["llm_usage_source"])

    def test_project_workspace_groups_objective_review_packets_by_round(self) -> None:
        fake_router = FakeLLMRouter("{}")
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        completed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Review-ready task",
            objective="Execution is complete",
            status=TaskStatus.COMPLETED,
        )
        self.store.create_task(completed_task)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.RESOLVED)

        self.service.queue_objective_review(self.objective.id, async_mode=False)
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        review = objective_payload["promotion_review"]

        self.assertEqual(1, len(review["review_rounds"]))
        latest_round = review["review_rounds"][0]
        self.assertEqual(1, latest_round["round_number"])
        self.assertEqual(2, latest_round["packet_count"])
        self.assertEqual(1, latest_round["verdict_counts"]["concern"])
        self.assertEqual(1, latest_round["verdict_counts"]["pass"])
        self.assertEqual(1, latest_round["remediation_counts"]["total"])
        self.assertEqual("improving", latest_round["packets"][-1]["progress_status"])
        self.assertTrue(latest_round["review_cycle_artifact"]["record_id"])

    def test_objective_review_auto_starts_next_round_after_remediation_completes(self) -> None:
        fake_router = FakeLLMRouter("{}")
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        completed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Review-ready task",
            objective="Execution is complete",
            status=TaskStatus.COMPLETED,
        )
        self.store.create_task(completed_task)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.RESOLVED)

        self.service.queue_objective_review(self.objective.id, async_mode=False)
        remediation_tasks = [
            task for task in self.store.list_tasks(self.project.id)
            if task.objective_id == self.objective.id and task.strategy == "objective_review_remediation"
        ]
        self.assertEqual(1, len(remediation_tasks))
        remediation_task = remediation_tasks[0]
        self.store.update_task_status(remediation_task.id, TaskStatus.COMPLETED)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.PLANNING)

        self.service._maybe_resume_objective_review(self.objective.id)
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        review = objective_payload["promotion_review"]

        self.assertEqual(2, len(review["review_rounds"]))
        self.assertEqual(2, review["review_rounds"][0]["round_number"])
        self.assertEqual("objective_review", review["review_rounds"][0]["packets"][0]["source"])
        self.assertEqual(4, review["objective_review_packet_count"])

    def test_completed_remediation_persists_worker_response_record(self) -> None:
        fake_router = FakeLLMRouter("{}")
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        completed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Review-ready task",
            objective="Execution is complete",
            status=TaskStatus.COMPLETED,
        )
        self.store.create_task(completed_task)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.RESOLVED)

        self.service.queue_objective_review(self.objective.id, async_mode=False)
        remediation_task = next(
            task for task in self.store.list_tasks(self.project.id)
            if task.objective_id == self.objective.id and task.strategy == "objective_review_remediation"
        )
        remediation_run = Run(
            id=new_id("run"),
            task_id=remediation_task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary="Produced the requested review packet artifact.",
        )
        self.store.create_run(remediation_run)
        artifact = Artifact(
            id=new_id("artifact"),
            run_id=remediation_run.id,
            kind="objective_review_packet",
            path=str(self.workspace_root / "review-packet.json"),
            summary="Objective QA review packet",
        )
        self.store.create_artifact(artifact)
        self.store.update_task_status(remediation_task.id, TaskStatus.COMPLETED)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.PLANNING)

        self.service._maybe_resume_objective_review(self.objective.id)

        response_records = self.store.list_context_records(
            objective_id=self.objective.id,
            record_type="objective_review_worker_response",
        )
        self.assertEqual(1, len(response_records))
        self.assertEqual("objective_review_packet", response_records[0].metadata["required_artifact_type"])
        self.assertEqual(str(artifact.id), response_records[0].metadata["record_id"])

    def test_objective_review_prompt_includes_prior_round_context(self) -> None:
        fake_router = FakeLLMRouter("{}")
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        completed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Review-ready task",
            objective="Execution is complete",
            status=TaskStatus.COMPLETED,
        )
        self.store.create_task(completed_task)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.RESOLVED)

        self.service.queue_objective_review(self.objective.id, async_mode=False)
        remediation_task = next(
            task for task in self.store.list_tasks(self.project.id)
            if task.objective_id == self.objective.id and task.strategy == "objective_review_remediation"
        )
        self.store.update_task_status(remediation_task.id, TaskStatus.COMPLETED)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.PLANNING)

        self.service._maybe_resume_objective_review(self.objective.id)

        self.assertIn("Previous review rounds:", fake_router.prompts[-1])
        self.assertIn("\"progress_status\"", fake_router.prompts[-1])
        self.assertIn("closure_criteria", fake_router.prompts[-1])
        self.assertIn("evidence_required", fake_router.prompts[-1])
        self.assertIn("repeat_reason", fake_router.prompts[-1])

    def test_parse_objective_review_response_rejects_vague_non_pass_packet(self) -> None:
        response = json.dumps(
            {
                "summary": "Objective review generated.",
                "packets": [
                    {
                        "reviewer": "QA agent",
                        "dimension": "unit_test_coverage",
                        "verdict": "concern",
                        "progress_status": "improving",
                        "severity": "medium",
                        "owner_scope": "tests",
                        "summary": "Testing should improve before promotion.",
                        "findings": ["Need more testing."],
                        "evidence": ["Current tests are not enough."],
                        "required_artifact_type": "objective_review_packet",
                        "artifact_schema": {
                            "type": "objective_review_packet",
                            "description": "Persist a QA review packet with concrete test evidence.",
                            "required_fields": ["review_id", "reviewer", "dimension", "verdict", "artifacts"],
                        },
                        "closure_criteria": "Improve testing before promotion.",
                        "evidence_required": "More evidence.",
                        "repeat_reason": "This is still improving.",
                    }
                ],
            }
        )

        parsed = self.service._parse_objective_review_response(response)

        self.assertIsNone(parsed)

    def test_queue_objective_review_falls_back_when_llm_packets_fail_policy_validation(self) -> None:
        fake_router = InvalidObjectiveReviewRouter(
            json.dumps(
                {
                    "summary": "Objective review generated.",
                    "packets": [
                        {
                            "reviewer": "QA agent",
                            "dimension": "unit_test_coverage",
                            "verdict": "concern",
                            "progress_status": "improving",
                            "severity": "medium",
                            "owner_scope": "tests",
                            "summary": "Testing should improve before promotion.",
                            "findings": ["Need more testing."],
                            "evidence": ["Current tests are not enough."],
                            "required_artifact_type": "objective_review_packet",
                            "artifact_schema": {
                                "type": "objective_review_packet",
                                "description": "Persist a QA review packet with concrete test evidence.",
                                "required_fields": ["review_id", "reviewer", "dimension", "verdict", "artifacts"],
                            },
                            "closure_criteria": "Improve testing before promotion.",
                            "evidence_required": "More evidence.",
                            "repeat_reason": "This is still improving.",
                        }
                    ],
                }
            )
        )
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        completed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Review-ready task",
            objective="Execution is complete",
            status=TaskStatus.COMPLETED,
        )
        self.store.create_task(completed_task)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.RESOLVED)

        self.service.queue_objective_review(self.objective.id, async_mode=False)
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == self.objective.id)
        review = objective_payload["promotion_review"]

        self.assertEqual(3, review["objective_review_packet_count"])
        self.assertEqual(
            {"intent_fidelity", "unit_test_coverage", "code_structure"},
            {packet["dimension"] for packet in review["review_rounds"][0]["packets"]},
        )
        qa_packet = next(packet for packet in review["review_rounds"][0]["packets"] if packet["dimension"] == "unit_test_coverage")
        self.assertEqual("objective review evidence", qa_packet["owner_scope"])
        self.assertTrue(qa_packet["closure_criteria"])
        self.assertTrue(qa_packet["evidence_required"])

    def test_validate_objective_review_packet_rejects_repeated_artifact_concern_when_completed_round_exists(self) -> None:
        objective_payload = {
            "review_rounds": [
                {
                    "completed_at": "2026-03-19T20:00:00+00:00",
                    "packet_count": 7,
                    "status": "ready_for_rerun",
                    "verdict_counts": {"pass": 4, "concern": 3, "remediation_required": 0},
                    "remediation_counts": {"total": 3, "completed": 3, "active": 0, "pending": 0, "failed": 0},
                }
            ]
        }

        validated = self.service._validate_objective_review_packet(
            {
                "reviewer": "Board reviewer",
                "dimension": "intent_fidelity",
                "verdict": "concern",
                "progress_status": "improving",
                "severity": "medium",
                "owner_scope": "objective review orchestration",
                "summary": "The round is improving but still lacks proof.",
                "findings": ["The board still wants the completed round artifact."],
                "evidence": ["The latest round just finished."],
                "required_artifact_type": "review_cycle_artifact",
                "artifact_schema": {
                    "type": "review_cycle_artifact",
                    "description": "Persist a completed objective review cycle artifact with terminal event evidence.",
                    "required_fields": ["review_id", "start_event", "packet_persistence_events", "terminal_event", "linked_outcome"],
                },
                "closure_criteria": "Record one completed objective review round for this objective with at least 7 persisted reviewer packets, non-zero verdict_counts, and remediation linkage.",
                "evidence_required": "A persisted objective review artifact for round 8 or later showing packets[], verdict_counts, completed_at, and remediation linkage.",
                "repeat_reason": "Repeated because the board still wants the completed round artifact.",
            },
            objective_payload=objective_payload,
        )

        self.assertIsNone(validated)

    def test_stale_objective_review_is_interrupted_and_restarted(self) -> None:
        fake_router = FakeLLMRouter("{}")
        self.ctx.interrogation_service = SimpleNamespace(llm_router=fake_router)
        self.service = HarnessUIDataService(self.ctx)
        completed_task = Task(
            id=new_id("task"),
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Review-ready task",
            objective="Execution is complete",
            status=TaskStatus.COMPLETED,
        )
        self.store.create_task(completed_task)
        self.store.update_objective_status(self.objective.id, ObjectiveStatus.RESOLVED)
        stale_review_id = new_id("objective_review")
        started = ContextRecord(
            id=new_id("context"),
            record_type="objective_review_started",
            project_id=self.project.id,
            objective_id=self.objective.id,
            visibility="operator_visible",
            author_type="system",
            content="Started automatic objective promotion review.",
            metadata={"review_id": stale_review_id},
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10),
        )
        self.store.create_context_record(started)

        self.service._maybe_resume_objective_review(self.objective.id)

        review = self.service._objective_review_state(self.objective.id)
        self.assertIn(review["status"], {"running", "completed"})
        self.assertNotEqual(stale_review_id, review["review_id"])
        failed_records = [
            record for record in self.store.list_context_records(objective_id=self.objective.id, record_type="objective_review_failed")
        ]
        self.assertTrue(any(str(record.metadata.get("review_id") or "") == stale_review_id for record in failed_records))

    def test_stale_atomic_generation_is_recovered_for_finished_mermaid(self) -> None:
        recovering_objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Recover atomic generation",
            summary="Ensure interrupted decomposition resumes",
        )
        self.store.create_objective(recovering_objective)
        self.service.update_intent_model(
            recovering_objective.id,
            intent_summary="Operator wants recovery if atomic generation is interrupted",
            success_definition="Atomic units resume after an interrupted generation",
            non_negotiables=["Resume from accepted flowchart"],
            frustration_signals=["No units appear"],
        )
        self.service.complete_interrogation_review(recovering_objective.id)
        self.store.create_mermaid_artifact(
            MermaidArtifact(
                id=new_id("diagram"),
                objective_id=recovering_objective.id,
                diagram_type="workflow_control",
                version=1,
                status=MermaidStatus.FINISHED,
                summary="Accepted control flow",
                content="flowchart TD\nA[Intent]-->B[Plan]-->C[Review]",
                required_for_execution=True,
            )
        )
        start = ContextRecord(
            id=new_id("context"),
            record_type="atomic_generation_started",
            project_id=self.project.id,
            objective_id=recovering_objective.id,
            visibility="operator_visible",
            author_type="system",
            content="Started generating atomic units from Mermaid v1.",
            metadata={"generation_id": "atomic_generation_stale", "diagram_version": 1},
        )
        self.store.create_context_record(start)
        stale_state = self.service._atomic_generation_state(recovering_objective.id)
        self.service._mark_atomic_generation_interrupted(recovering_objective, stale_state)

        result = self.service.queue_atomic_generation(recovering_objective.id, async_mode=False)
        payload = self.service.project_workspace(self.project.id)
        objective_payload = next(item for item in payload["objectives"] if item["id"] == recovering_objective.id)

        self.assertEqual("completed", result["atomic_generation"]["status"])
        self.assertNotEqual("atomic_generation_stale", objective_payload["atomic_generation"]["generation_id"])
        self.assertIsInstance(objective_payload["atomic_units"], list)

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
        self.assertIsInstance(objective_payload["atomic_units"], list)

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


class BackgroundSupervisorCoordinatorTests(unittest.TestCase):
    def test_start_uses_unbounded_idle_watch_mode(self) -> None:
        called = threading.Event()
        seen: dict[str, object] = {}

        class _FakeEngine:
            worker = SimpleNamespace()

            def supervise(self, **kwargs):
                seen.update(kwargs)
                called.set()
                return SimpleNamespace(
                    processed_count=0,
                    exit_reason="idle",
                )

        coordinator = BackgroundSupervisorCoordinator()

        started = coordinator.start("project_12345678", _FakeEngine(), watch=True)

        self.assertTrue(started)
        self.assertTrue(called.wait(timeout=2))
        self.assertTrue(seen["watch"])
        self.assertIsNone(seen["max_idle_cycles"])


if __name__ == "__main__":
    unittest.main()
