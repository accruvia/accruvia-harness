from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import Event, Project, TaskStatus, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.interrogation import HarnessQueryService, InterrogationService
from accruvia_harness.llm import LLMExecutionResult
from accruvia_harness.policy import WorkResult
from accruvia_harness.store import SQLiteHarnessStore
from accruvia_harness.telemetry import TelemetrySink
from accruvia_harness.workers import LocalArtifactWorker


class MissingArtifactWorker(LocalArtifactWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text("plan only\n", encoding="utf-8")
        return WorkResult(
            summary="Recorded partial artifacts.",
            artifacts=[("plan", str(plan_path), "Plan only")],
            outcome="failed",
            diagnostics={"reason": "missing_report"},
        )


class HarnessQueryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(base / "harness.db")
        self.store.initialize()
        self.telemetry = TelemetrySink(base / "telemetry")
        self.engine = HarnessEngine(
            store=self.store,
            workspace_root=base / "workspace",
            telemetry=self.telemetry,
        )
        self.project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(self.project)
        self.query = HarnessQueryService(self.store, telemetry=self.telemetry)

    def test_portfolio_summary_reports_project_metrics(self) -> None:
        task = self.engine.import_issue_task(
            project_id=self.project.id,
            issue_id="500",
            title="Summary task",
            objective="Exercise summary output",
            priority=100,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.engine.run_until_stable(task.id)

        summary = self.query.portfolio_summary()

        self.assertEqual(1, summary["project_count"])
        self.assertEqual(1, summary["projects"][0]["metrics"]["tasks_by_status"]["completed"])

    def test_task_report_includes_runs_and_events(self) -> None:
        task = self.engine.import_issue_task(
            project_id=self.project.id,
            issue_id="501",
            title="Task report",
            objective="Exercise task report",
            priority=100,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.engine.run_until_stable(task.id)

        report = self.query.task_report(task.id)

        self.assertEqual(task.id, report["task"]["id"])
        self.assertEqual(1, len(report["runs"]))
        self.assertGreaterEqual(len(report["events"]), 3)

    def test_portfolio_summary_includes_retry_and_follow_on_metrics(self) -> None:
        failing_engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-retries",
            worker=MissingArtifactWorker(),
        )
        task = failing_engine.import_issue_task(
            project_id=self.project.id,
            issue_id="502",
            title="Retry task",
            objective="Cause retries",
            priority=100,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        runs = failing_engine.run_until_stable(task.id)
        failing_engine.create_follow_on_task(
            parent_task_id=task.id,
            source_run_id=runs[-1].id,
            title="Follow-on",
            objective="Handle failure",
        )

        summary = self.query.portfolio_summary()
        metrics = summary["global_metrics"]

        self.assertGreater(metrics["retry_rate"], 0.0)
        self.assertEqual(1, metrics["follow_on_task_count"])

    def test_operations_report_includes_profile_metrics_and_pending_affirmations(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project.id,
            title="Ops report",
            objective="Exercise ops report output",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="python",
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = self.engine.run_once(task.id)
        self.engine.review_promotion(task.id, run.id)

        report = self.query.operations_report(self.project.id)

        self.assertEqual(1, report["metrics"]["pending_promotions"])
        self.assertEqual(1, report["metrics"]["tasks_by_validation_profile"]["python"])
        self.assertEqual(1, len(report["pending_affirmations"]))

    def test_task_lineage_reports_ancestors_and_children(self) -> None:
        parent = self.engine.create_task_with_policy(
            project_id=self.project.id,
            title="Parent",
            objective="Parent task",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = self.engine.run_once(parent.id)
        child = self.engine.create_follow_on_task(
            parent_task_id=parent.id,
            source_run_id=run.id,
            title="Child",
            objective="Child task",
        )
        grandchild = self.engine.create_follow_on_task(
            parent_task_id=child.id,
            source_run_id=run.id,
            title="Grandchild",
            objective="Grandchild task",
        )

        lineage = self.query.task_lineage(child.id)

        self.assertEqual(parent.id, lineage["ancestors"][0]["id"])
        self.assertEqual(grandchild.id, lineage["children"][0]["task"]["id"])

    def test_dashboard_report_includes_telemetry_rollups(self) -> None:
        task = self.engine.import_issue_task(
            project_id=self.project.id,
            issue_id="503",
            title="Dashboard task",
            objective="Exercise dashboard output",
            priority=100,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.engine.run_until_stable(task.id)

        dashboard = self.query.dashboard_report(self.project.id)

        self.assertIn("telemetry", dashboard)
        self.assertIn("dashboard", dashboard)
        self.assertIn("slowest_operations_ms", dashboard["dashboard"])

    def test_context_packet_scopes_leases_by_project(self) -> None:
        other_project = Project(id=new_id("project"), name="other", description="Other project")
        self.store.create_project(other_project)
        task_a = self.engine.create_task_with_policy(
            project_id=self.project.id,
            title="Lease A",
            objective="Lease A",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        task_b = self.engine.create_task_with_policy(
            project_id=other_project.id,
            title="Lease B",
            objective="Lease B",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.store.acquire_task_lease("worker-a", 300, project_id=self.project.id)
        self.store.acquire_task_lease("worker-b", 300, project_id=other_project.id)

        packet = self.query.context_packet(self.project.id)

        self.assertEqual(1, len(packet["leases"]))
        self.assertEqual(task_a.id, packet["leases"][0]["task_id"])
        self.assertNotEqual(task_b.id, packet["leases"][0]["task_id"])

    def test_context_packet_includes_strategy_history_and_telemetry_summary(self) -> None:
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="project",
                entity_id=self.project.id,
                event_type="heartbeat_completed",
                payload={
                    "adapter_name": "generic",
                    "summary": "Created a focused backlog slice",
                    "issue_creation_needed": True,
                    "proposed_task_count": 2,
                    "created_task_count": 1,
                    "skipped_task_count": 1,
                    "next_heartbeat_seconds": 1800,
                },
            )
        )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="project",
                entity_id=self.project.id,
                event_type="heartbeat_scheduled",
                payload={"interval_seconds": 1800, "source": "default"},
            )
        )
        self.telemetry.metric("run_started", 1)
        with self.telemetry.timed("heartbeat_analysis", project_id=self.project.id):
            pass
        self.telemetry.warn("llm_executor_failure", "provider timeout", backend="claude")

        packet = self.query.context_packet(self.project.id)

        self.assertEqual(1, packet["strategy_history"]["heartbeat_count"])
        self.assertEqual(1, packet["strategy_history"]["tasks_created_from_heartbeats"])
        self.assertEqual(
            "Created a focused backlog slice",
            packet["strategy_history"]["recent_heartbeats"][0]["summary"],
        )
        self.assertEqual(1800, packet["strategy_history"]["recent_heartbeats"][0]["next_heartbeat_seconds"])
        self.assertEqual(1.0, packet["telemetry_summary"]["metric_totals"]["run_started"])
        self.assertEqual(1, packet["telemetry_summary"]["span_counts"]["heartbeat_analysis"])
        self.assertEqual("llm_executor_failure", packet["telemetry_summary"]["recent_warnings"][0]["category"])
        self.assertEqual("idle", packet["loop_status"]["status"])
        self.assertEqual(1800, packet["loop_status"]["heartbeat_interval_seconds"])

    def test_project_summary_reports_healthy_idle_after_recent_completion(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project.id,
            title="Healthy idle task",
            objective="Complete and idle cleanly",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.engine.run_until_stable(task.id)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="project",
                entity_id=self.project.id,
                event_type="heartbeat_completed",
                payload={"summary": "No new tasks are justified right now.", "next_heartbeat_seconds": 1800},
            )
        )
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="project",
                entity_id=self.project.id,
                event_type="heartbeat_scheduled",
                payload={"interval_seconds": 1800, "source": "default"},
            )
        )

        summary = self.query.project_summary(self.project.id)

        self.assertTrue(summary["loop_status"]["healthy_idle"])
        self.assertEqual("healthy_idle", summary["loop_status"]["status"])
        self.assertEqual(task.id, summary["loop_status"]["last_completed_task_id"])

    def test_context_packet_includes_recent_operator_nudges(self) -> None:
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="project",
                entity_id=self.project.id,
                event_type="operator_nudge",
                payload={"note": "Prioritize onboarding and DX", "author": "tester"},
            )
        )

        packet = self.query.context_packet(self.project.id)

        self.assertEqual("Prioritize onboarding and DX", packet["operator_nudges"][0]["note"])
        self.assertEqual("tester", packet["operator_nudges"][0]["author"])

    def test_dashboard_queue_depth_counts_only_pending_and_active(self) -> None:
        completed = self.engine.create_task_with_policy(
            project_id=self.project.id,
            title="Completed task",
            objective="Completed task",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.engine.run_until_stable(completed.id)
        pending = self.engine.create_task_with_policy(
            project_id=self.project.id,
            title="Pending task",
            objective="Pending task",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        active = self.engine.create_task_with_policy(
            project_id=self.project.id,
            title="Active task",
            objective="Active task",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.store.update_task_status(active.id, TaskStatus.ACTIVE)

        dashboard = self.query.dashboard_report(self.project.id)

        self.assertEqual(2, dashboard["dashboard"]["queue_depth"])
        self.assertEqual(3, dashboard["dashboard"]["total_tasks"])

    def test_query_service_uses_read_only_store_wrapper(self) -> None:
        with self.assertRaises(AttributeError):
            self.query.store.update_task_status("task_x", None)

    def test_query_service_read_only_store_blocks_low_level_connection_access(self) -> None:
        with self.assertRaises(AttributeError):
            self.query.store.connect()

    def test_explanation_artifacts_use_unique_paths_per_call(self) -> None:
        class FakeExecutor:
            backend_name = "fake"

            def execute(self, invocation):
                prompt_path = invocation.run_dir / "llm_prompt.txt"
                response_path = invocation.run_dir / "llm_response.md"
                prompt_path.write_text(invocation.prompt, encoding="utf-8")
                response_path.write_text("explanation", encoding="utf-8")
                return LLMExecutionResult(
                    backend="fake",
                    response_text="explanation",
                    prompt_path=prompt_path,
                    response_path=response_path,
                    diagnostics={},
                )

        class FakeRouter:
            def resolve(self):
                return FakeExecutor(), "fake"

        task = self.engine.import_issue_task(
            project_id=self.project.id,
            issue_id="504",
            title="Explain me",
            objective="Exercise explanation output",
            priority=100,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.engine.run_until_stable(task.id)

        service = InterrogationService(
            query_service=self.query,
            workspace_root=Path(self.temp_dir.name) / "workspace",
            llm_router=FakeRouter(),
            telemetry=self.telemetry,
        )

        first = service.explain_task(task.id)
        second = service.explain_task(task.id)

        self.assertNotEqual(first["explanation_path"], second["explanation_path"])
        self.assertTrue(Path(first["explanation_path"]).exists())
        self.assertTrue(Path(second["explanation_path"]).exists())
