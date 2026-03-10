from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import Project, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.llm import build_llm_router
from accruvia_harness.policy import WorkResult
from accruvia_harness.workers import LocalArtifactWorker
from accruvia_harness.store import SQLiteHarnessStore


class MissingArtifactWorker(LocalArtifactWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text("plan only\n", encoding="utf-8")
        return WorkResult(
            summary="Recorded only a partial artifact set.",
            artifacts=[("plan", str(plan_path), "Plan artifact only")],
        )


class PromotionBlockedWorker(LocalArtifactWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text("plan\n", encoding="utf-8")
        report_path = run_dir / "report.json"
        report_path.write_text(
            json.dumps(
                {
                    "changed_files": ["src/accruvia_client/runner.py"],
                    "test_files": [],
                    "compile_check": {"passed": True},
                    "test_check": {"passed": False},
                    "promotion_blocked": True,
                    "promotion_block_reason": "Generated candidate lacks required test coverage.",
                    "follow_on_title": "Add missing test coverage",
                    "follow_on_objective": "Add the missing tests and regenerate the candidate.",
                }
            ),
            encoding="utf-8",
        )
        return WorkResult(
            summary="Recorded blocked promotion artifacts.",
            artifacts=[
                ("plan", str(plan_path), "Plan artifact"),
                ("report", str(report_path), "Blocked report artifact"),
            ],
        )


class HarnessEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(base / "harness.db")
        self.store.initialize()
        self.engine = HarnessEngine(
            store=self.store,
            workspace_root=base / "workspace",
        )
        project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(project)
        self.project_id = project.id

    def test_run_once_completes_when_required_artifacts_exist(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Build first loop",
            objective="Produce required artifacts",
            priority=200,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id="456",
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )

        run = self.engine.run_once(task.id)
        artifacts = self.store.list_artifacts(run.id)
        evaluations = self.store.list_evaluations(run.id)
        decisions = self.store.list_decisions(run.id)
        task_after = self.store.get_task(task.id)

        self.assertEqual("completed", run.status.value)
        self.assertEqual(["plan", "report"], sorted(artifact.kind for artifact in artifacts))
        self.assertEqual("acceptable", evaluations[0].verdict)
        self.assertEqual("promote", decisions[0].action.value)
        assert task_after is not None
        self.assertEqual("gitlab_issue", task_after.external_ref_type)
        self.assertEqual("456", task_after.external_ref_id)
        self.assertEqual("completed", task_after.status.value)

    def test_run_until_stable_fails_after_retry_budget_is_exhausted(self) -> None:
        failing_engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-retry",
            worker=MissingArtifactWorker(),
        )
        task = failing_engine.create_task_with_policy(
            project_id=self.project_id,
            title="Retry until failed",
            objective="Exercise bounded retries",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id="457",
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )

        runs = failing_engine.run_until_stable(task.id)
        task_after = self.store.get_task(task.id)
        last_run = runs[-1]
        last_eval = self.store.list_evaluations(last_run.id)[0]
        last_decision = self.store.list_decisions(last_run.id)[0]

        self.assertEqual(2, len(runs))
        self.assertEqual("failed", task_after.status.value if task_after else None)
        self.assertEqual("failed", last_run.status.value)
        self.assertEqual("incomplete", last_eval.verdict)
        self.assertEqual("fail", last_decision.action.value)
        self.assertEqual(["report"], last_eval.details["missing_required_artifacts"])

    def test_run_once_emits_auditable_events(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Emit events",
            objective="Capture the control flow",
            priority=150,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id="458",
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )

        run = self.engine.run_once(task.id)
        task_events = self.store.list_events("task", task.id)
        run_events = self.store.list_events("run", run.id)

        self.assertEqual(
            ["task_created", "task_activated", "task_status_changed"],
            [event.event_type for event in task_events],
        )
        self.assertEqual("gitlab_issue", task_events[0].payload["external_ref_type"])
        self.assertEqual("458", task_events[0].payload["external_ref_id"])
        self.assertEqual(
            ["run_created", "planned", "worker_completed"],
            [event.event_type for event in run_events],
        )

    def test_process_queue_uses_priority_order(self) -> None:
        low = self.engine.import_issue_task(
            project_id=self.project_id,
            issue_id="460",
            title="Low priority",
            objective="Go second",
            priority=50,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        high = self.engine.import_issue_task(
            project_id=self.project_id,
            issue_id="461",
            title="High priority",
            objective="Go first",
            priority=500,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        processed = self.engine.process_queue(limit=2)

        self.assertEqual(2, len(processed))
        self.assertEqual(high.id, processed[0]["task"].id)
        self.assertEqual(low.id, processed[1]["task"].id)

    def test_import_issue_task_creates_gitlab_linked_task(self) -> None:
        task = self.engine.import_issue_task(
            project_id=self.project_id,
            issue_id="462",
            title="Imported issue",
            objective="Work imported from GitLab",
            priority=300,
            strategy="baseline",
            max_attempts=4,
            required_artifacts=["plan", "report"],
        )

        self.assertEqual("gitlab_issue", task.external_ref_type)
        self.assertEqual("462", task.external_ref_id)
        self.assertEqual(300, task.priority)

    def test_engine_accepts_injected_policy_components(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-injected",
            planner=self.engine.planner,
            worker=self.engine.worker,
            analyzer=self.engine.analyzer,
            decider=self.engine.decider,
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Injected policy",
            objective="Verify policy composition",
            priority=125,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)
        self.assertEqual("completed", run.status.value)

    def test_create_follow_on_task_preserves_lineage(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Parent task",
            objective="Generate follow-on work",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id="463",
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        run = self.engine.run_once(task.id)

        follow_on = self.engine.create_follow_on_task(
            parent_task_id=task.id,
            source_run_id=run.id,
            title="Follow-on task",
            objective="Handle discovered defect",
        )

        self.assertEqual(task.id, follow_on.parent_task_id)
        self.assertEqual(run.id, follow_on.source_run_id)
        self.assertEqual("463", follow_on.external_ref_id)

    def test_process_next_task_uses_and_releases_lease(self) -> None:
        task = self.engine.import_issue_task(
            project_id=self.project_id,
            issue_id="464",
            title="Lease-aware queue item",
            objective="Verify process-next leases work",
            priority=100,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        result = self.engine.process_next_task(worker_id="worker-a", lease_seconds=120)

        self.assertEqual(task.id, result["task"].id if result else None)
        self.assertEqual([], self.store.list_task_leases())

    def test_review_promotion_creates_pending_candidate(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Promotion pass",
            objective="Produce promotable candidate",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = self.engine.run_once(task.id)

        result = self.engine.review_promotion(task.id, run.id)

        self.assertEqual("pending", result.promotion.status.value)
        self.assertIsNone(result.follow_on_task_id)
        self.assertEqual("pending", self.store.latest_promotion(task.id).status.value)

    def test_affirm_promotion_approves_pending_candidate(self) -> None:
        config = HarnessConfig(
            db_path=Path(self.temp_dir.name) / "affirm.db",
            workspace_root=Path(self.temp_dir.name) / "workspace-affirm",
            log_path=Path(self.temp_dir.name) / "affirm.log",
            default_project_name="demo",
            default_repo="accruvia/accruvia",
            runtime_backend="local",
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="accruvia-harness",
            worker_backend="local",
            worker_command=None,
            llm_backend="command",
            llm_model=None,
            llm_command=f'bash "{Path(__file__).resolve().parent / "fixtures" / "fake_affirm_approve.sh"}"',
            llm_codex_command=None,
            llm_claude_command=None,
            llm_accruvia_client_command=None,
        )
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-affirm",
            llm_router=build_llm_router(config),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Promotion affirm",
            objective="Affirm a promotable candidate",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)
        engine.review_promotion(task.id, run.id)

        result = engine.affirm_promotion(task.id, run.id)

        self.assertEqual("approved", result.promotion.status.value)
        self.assertIn("affirmation", result.promotion.details)

    def test_review_promotion_rejects_and_creates_follow_on(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-promotion-blocked",
            worker=PromotionBlockedWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Promotion blocked",
            objective="Exercise promotion rejection",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id="465",
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)

        result = engine.review_promotion(task.id, run.id)
        follow_on = self.store.get_task(result.follow_on_task_id) if result.follow_on_task_id else None

        self.assertEqual("rejected", result.promotion.status.value)
        self.assertIsNotNone(follow_on)
        self.assertEqual(task.id, follow_on.parent_task_id if follow_on else None)
        self.assertEqual(run.id, follow_on.source_run_id if follow_on else None)
        self.assertEqual("failed", self.store.get_task(task.id).status.value)

    def test_review_promotion_dedupes_follow_on_for_same_run(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-promotion-dedupe",
            worker=PromotionBlockedWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Promotion blocked once",
            objective="Avoid duplicate follow-ons",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)

        first = engine.review_promotion(task.id, run.id)
        second = engine.review_promotion(task.id, run.id)

        self.assertEqual(first.follow_on_task_id, second.follow_on_task_id)

    def test_review_promotion_rejects_when_deterministic_test_evidence_is_missing(self) -> None:
        class NoTestEvidenceWorker(LocalArtifactWorker):
            def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
                run_dir = workspace_root / "runs" / run.id
                run_dir.mkdir(parents=True, exist_ok=True)
                plan_path = run_dir / "plan.txt"
                plan_path.write_text("plan\n", encoding="utf-8")
                report_path = run_dir / "report.json"
                report_path.write_text(
                    json.dumps(
                        {
                            "changed_files": ["src/example.py"],
                            "compile_check": {"passed": True},
                            "test_files": [],
                            "test_check": {"passed": False},
                        }
                    ),
                    encoding="utf-8",
                )
                return WorkResult(
                    summary="Recorded candidate without test evidence.",
                    artifacts=[
                        ("plan", str(plan_path), "Plan artifact"),
                        ("report", str(report_path), "Candidate report artifact"),
                    ],
                )

        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-no-tests",
            worker=NoTestEvidenceWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Missing test evidence",
            objective="Exercise deterministic validator rejection",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)

        result = engine.review_promotion(task.id, run.id)

        self.assertEqual("rejected", result.promotion.status.value)
        validator_names = [entry["validator"] for entry in result.promotion.details["validators"]]
        self.assertIn("test_evidence", validator_names)

    def test_affirm_promotion_rejects_pending_candidate(self) -> None:
        config = HarnessConfig(
            db_path=Path(self.temp_dir.name) / "reject.db",
            workspace_root=Path(self.temp_dir.name) / "workspace-reject-affirm",
            log_path=Path(self.temp_dir.name) / "reject.log",
            default_project_name="demo",
            default_repo="accruvia/accruvia",
            runtime_backend="local",
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="accruvia-harness",
            worker_backend="local",
            worker_command=None,
            llm_backend="command",
            llm_model=None,
            llm_command=f'bash "{Path(__file__).resolve().parent / "fixtures" / "fake_affirm_reject.sh"}"',
            llm_codex_command=None,
            llm_claude_command=None,
            llm_accruvia_client_command=None,
        )
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-reject-affirm",
            llm_router=build_llm_router(config),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Promotion reject",
            objective="Reject a pending promotion",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)
        engine.review_promotion(task.id, run.id)

        result = engine.affirm_promotion(task.id, run.id)

        self.assertEqual("rejected", result.promotion.status.value)
        self.assertIsNotNone(result.follow_on_task_id)
