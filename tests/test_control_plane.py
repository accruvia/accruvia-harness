from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import threading
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from accruvia_harness.commands.common import (
    build_context,
    desired_sa_watch_state_path,
    sa_watch_launch_state_path,
    sa_watch_runtime_state_path,
    clear_stack_restart_request,
    read_stack_restart_request,
    record_desired_supervisor_state,
    record_desired_ui_state,
    restart_api_process,
    restart_harness_process,
    startup_preflight,
    ui_runtime_state_path,
    update_ui_runtime_state,
)
from accruvia_harness.commands.control import handle_control_command
from accruvia_harness.commands.core import _worker_status_operator_text, handle_core_command
from accruvia_harness.config import HarnessConfig
from accruvia_harness.control_breadcrumbs import BreadcrumbWriter
from accruvia_harness.control_classifier import FailureClassifier
from accruvia_harness.control_plane import ControlPlane
from accruvia_harness.control_runtime import ControlRuntimeObserver
from accruvia_harness.control_watch import ControlWatchService
from accruvia_harness.context_control import ObjectiveExecutionGate
from accruvia_harness.services.queue_service import QueueService
from accruvia_harness.services.repository_promotion_service import LocalCIResult
from accruvia_harness.services.structural_fix_promotion_service import StructuralFixPromotionService
from accruvia_harness.services.task_service import TaskService
from accruvia_harness.services.workflow_service import WorkflowService
from accruvia_harness.llm import LLMExecutionResult, LLMRouter
from accruvia_harness.sa_watch import SAWatchRepairResult, SAWatchService
from accruvia_harness.domain import Artifact, ContextRecord, ControlEvent, ControlRecoveryAction, ControlWorkerRun, Objective, ObjectiveStatus, Project, PromotionRecord, PromotionStatus, Run, RunStatus, Task, TaskStatus, new_id
from accruvia_harness.store import SQLiteHarnessStore


class FakeExecutor:
    backend_name = "codex"

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.prompts: list[str] = []

    def execute(self, invocation):  # type: ignore[override]
        self.prompts.append(invocation.prompt)
        invocation.run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = invocation.run_dir / "prompt.txt"
        response_path = invocation.run_dir / "response.json"
        prompt_path.write_text(invocation.prompt, encoding="utf-8")
        response_path.write_text(self.response_text, encoding="utf-8")
        return LLMExecutionResult(
            backend=self.backend_name,
            response_text=self.response_text,
            prompt_path=prompt_path,
            response_path=response_path,
            diagnostics={},
        )


class FakeRunner:
    def __init__(self) -> None:
        self.ran_task_ids: list[str] = []

    def run_once(self, task_id: str, progress_callback=None):  # type: ignore[override]
        self.ran_task_ids.append(task_id)
        if progress_callback is not None:
            progress_callback({"type": "run_created", "task_id": task_id, "run_id": f"run_{task_id}", "attempt": 1})

        class _Run:
            id = f"run_{task_id}"
            summary = "ok"

            class status:
                value = "completed"

        return _Run()


class FakeStableEngine:
    def __init__(self, store: SQLiteHarnessStore, *, complete: bool = True) -> None:
        self.store = store
        self.complete = complete
        self.ran_task_ids: list[str] = []

    def run_until_stable(self, task_id: str, progress_callback=None, post_task_callback=None):  # type: ignore[override]
        self.ran_task_ids.append(task_id)
        if progress_callback is not None:
            progress_callback({"type": "run_created", "task_id": task_id, "run_id": f"run_{task_id}", "attempt": 1})
        self.store.update_task_status(task_id, TaskStatus.COMPLETED if self.complete else TaskStatus.FAILED)
        updated = self.store.get_task(task_id)
        if post_task_callback is not None and updated is not None:
            post_task_callback(updated)
        if progress_callback is not None and updated is not None:
            progress_callback(
                {
                    "type": "task_finished",
                    "task_id": updated.id,
                    "task_title": updated.title,
                    "project_id": updated.project_id,
                    "status": updated.status.value,
                    "run_id": f"run_{task_id}",
                    "run_status": updated.status.value,
                    "summary": "ok",
                }
            )
        return []


@dataclass
class _FakeSuperviseResult:
    processed_count: int = 0
    processed_task_ids: list[str] | None = None
    exit_reason: str = "idle"
    heartbeat_count: int = 0
    review_check_count: int = 0
    idle_cycles: int = 0
    slept_seconds: float = 0.0


class FailureClassifierTests(unittest.TestCase):
    def test_classifies_rate_limit_without_retry(self) -> None:
        result = FailureClassifier().classify("API rate limit reached. Provider returned 429.")

        self.assertEqual("provider_rate_limit", result.classification)
        self.assertFalse(result.retry_recommended)
        self.assertEqual(1800, result.cooldown_seconds)

    def test_classifies_timeout_as_retryable(self) -> None:
        result = FailureClassifier().classify("Worker timed out after 1800 seconds.")

        self.assertEqual("timeout", result.classification)
        self.assertTrue(result.retry_recommended)

    def test_classifies_missing_required_artifacts_as_artifact_contract_failure(self) -> None:
        result = FailureClassifier().classify("Run is missing required artifacts. Retry budget exhausted.")

        self.assertEqual("artifact_contract_failure", result.classification)
        self.assertTrue(result.retry_recommended)


class BreadcrumbWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace_root = root / "workspace"
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()

    def test_writes_bundle_and_indexes_it(self) -> None:
        writer = BreadcrumbWriter(self.store, self.workspace_root)

        bundle_dir = writer.write_bundle(
            entity_type="task",
            entity_id="task_123",
            meta={"task_id": "task_123"},
            evidence={"checks": [{"name": "tests", "result": "pass"}]},
            decision={"classification": "timeout", "retry_recommended": True},
            worker_run_id="run_123",
            summary="Tests passed but worker timed out after validation.",
        )

        self.assertTrue((bundle_dir / "meta.json").exists())
        self.assertTrue((bundle_dir / "evidence.json").exists())
        self.assertTrue((bundle_dir / "decision.json").exists())
        self.assertTrue((bundle_dir / "summary.txt").exists())

        indexed = self.store.list_control_breadcrumbs(entity_type="task", entity_id="task_123")
        self.assertEqual(1, len(indexed))
        self.assertEqual("run_123", indexed[0].worker_run_id)
        self.assertEqual("timeout", indexed[0].classification)


class _VersionHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/api/version":
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps({"version": "test"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class ControlRestartHelperTests(unittest.TestCase):
    def test_restart_harness_clears_stale_stop_request_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = HarnessConfig.from_env(root / "harness.db", root / "workspace", root / "harness.log")
            supervisor_dir = config.db_path.parent / "supervisors"
            supervisor_dir.mkdir(parents=True, exist_ok=True)
            stop_request = supervisor_dir / "stop.request"
            stop_request.write_text("graceful-stop-requested\n", encoding="utf-8")
            record_desired_supervisor_state(
                config,
                project_id=None,
                worker_id="test-supervisor",
                watch=True,
                lease_seconds=300,
                idle_sleep_seconds=5.0,
                max_idle_cycles=None,
                max_iterations=None,
                heartbeat_project_ids=[],
                heartbeat_interval_seconds=None,
                heartbeat_all_projects=False,
                review_check_enabled=True,
                review_check_interval_seconds=28800,
            )

            with patch("accruvia_harness.commands.common.subprocess.Popen") as popen:
                popen.return_value.pid = 12345
                result = restart_harness_process(config)

            self.assertIsNotNone(result)
            self.assertFalse(stop_request.exists())
            popen.assert_called_once()

    def test_restart_api_reuses_live_runtime_process_instead_of_spawning_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = HarnessConfig.from_env(root / "harness.db", root / "workspace", root / "harness.log")
            record_desired_ui_state(
                config,
                host="127.0.0.1",
                port=9100,
                open_browser=False,
                project_ref=None,
            )
            update_ui_runtime_state(
                config,
                host="127.0.0.1",
                preferred_port=9100,
                resolved_port=9100,
                project_ref=None,
            )

            with patch("accruvia_harness.commands.common.subprocess.Popen") as popen:
                result = restart_api_process(config)

            self.assertEqual(os.getpid(), result["pid"] if result is not None else None)
            self.assertTrue(bool(result and result.get("existing")))
            popen.assert_not_called()

    def test_startup_preflight_clears_stale_restart_request_and_dead_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = HarnessConfig.from_env(root / "harness.db", root / "workspace", root / "harness.log")
            store = SQLiteHarnessStore(config.db_path)
            store.initialize()
            control_dir = config.db_path.parent / "control"
            control_dir.mkdir(parents=True, exist_ok=True)
            (control_dir / "restart_stack_request.json").write_text(
                json.dumps({"reason": "sa_structural_fix_completed", "task_id": "task_123"}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            ui_runtime_state_path(config).write_text(json.dumps({"pid": 999999}), encoding="utf-8")
            sa_watch_runtime_state_path(config).write_text(json.dumps({"pid": 999999, "mode": "idle"}), encoding="utf-8")
            sa_watch_launch_state_path(config).write_text(
                json.dumps({"launcher_pid": 999999, "pid": 999998, "created_at": 1.0, "interval_seconds": 300.0}),
                encoding="utf-8",
            )

            result = startup_preflight(config, store)

            self.assertTrue(result["stale_restart_request_cleared"])
            self.assertTrue(result["stale_ui_runtime_cleared"])
            self.assertTrue(result["stale_sa_watch_runtime_cleared"])
            self.assertTrue(result["stale_sa_watch_launch_cleared"])
            self.assertIsNone(read_stack_restart_request(config))
            self.assertFalse(ui_runtime_state_path(config).exists())
            self.assertFalse(sa_watch_runtime_state_path(config).exists())
            self.assertFalse(sa_watch_launch_state_path(config).exists())

    def test_startup_preflight_prunes_dead_supervisor_records_and_stop_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = HarnessConfig.from_env(root / "harness.db", root / "workspace", root / "harness.log")
            store = SQLiteHarnessStore(config.db_path)
            store.initialize()
            supervisor_dir = config.db_path.parent / "supervisors"
            supervisor_dir.mkdir(parents=True, exist_ok=True)
            stale_record = supervisor_dir / "999999.json"
            stale_record.write_text(json.dumps({"pid": 999999, "worker_id": "stale"}), encoding="utf-8")
            stop_request = supervisor_dir / "stop.request"
            stop_request.write_text("graceful-stop-requested\n", encoding="utf-8")

            result = startup_preflight(config, store)

            self.assertEqual([999999], result["stale_supervisor_records"])
            self.assertTrue(result["stop_request_cleared"])
            self.assertFalse(stale_record.exists())
            self.assertFalse(stop_request.exists())


class ControlWatchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace_root = root / "workspace"
        self.supervisor_dir = root / "supervisors"
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()
        self.control_plane = ControlPlane(self.store)
        self.control_plane.turn_on()
        self.watch = ControlWatchService(
            self.store,
            self.control_plane,
            FailureClassifier(),
            BreadcrumbWriter(self.store, self.workspace_root),
            supervisor_control_dir=self.supervisor_dir,
        )

    def _create_task(self, *, status: TaskStatus = TaskStatus.PENDING, required_artifacts: list[str] | None = None) -> Task:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Watch task",
            objective="Watch objective",
            required_artifacts=required_artifacts or ["plan", "report"],
            status=status,
        )
        self.store.create_task(task)
        return task

    def _create_run(self, task: Task, *, status: RunStatus = RunStatus.WORKING, updated_at: datetime | None = None) -> Run:
        run = Run(
            id=new_id("run"),
            task_id=task.id,
            status=status,
            attempt=1,
            summary="watch",
            updated_at=updated_at or datetime.now(UTC),
        )
        self.store.create_run(run)
        return run

    def test_watch_detects_no_active_tasks_while_work_exists(self) -> None:
        task = self._create_task(status=TaskStatus.PENDING)
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(minutes=3)).isoformat(), task.id),
            )

        result = self.watch.run_once()

        self.assertTrue(result["stuck"])
        self.assertIn("No active tasks while work exists", result["matched_rules"])
        self.assertIn(task.id, result["affected_task_ids"])

    def test_watch_detects_recent_stalled_objective(self) -> None:
        project = Project(id=new_id("project"), name="objective-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Stalled objective",
            summary="stalled",
            status=ObjectiveStatus.PLANNING,
        )
        self.store.create_objective(objective)
        self.store.create_control_event(
            ControlEvent(
                id=new_id("control_event"),
                event_type="objective_stalled",
                entity_type="objective",
                entity_id=objective.id,
                producer="test",
                payload={"objective_id": objective.id},
                idempotency_key=new_id("event_key"),
            )
        )

        result = self.watch.run_once()

        self.assertTrue(result["stuck"])
        self.assertIn("Stalled objective exists", result["matched_rules"])

    def test_watch_detects_active_task_with_only_liveness_noise(self) -> None:
        task = self._create_task(status=TaskStatus.ACTIVE)
        run = self._create_run(task)
        lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO task_leases (task_id, worker_id, lease_expires_at, created_at) VALUES (?, ?, ?, ?)",
                (task.id, "restart-worker", lease_expires_at.isoformat(), datetime.now(UTC).isoformat()),
            )
        self.supervisor_dir.mkdir(parents=True, exist_ok=True)
        (self.supervisor_dir / "123.json").write_text(
            json.dumps({"pid": os.getpid(), "project_id": task.project_id, "worker_id": "restart-worker"}),
            encoding="utf-8",
        )
        run_dir = self.workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        heartbeat = run_dir / "worker.heartbeat.json"
        heartbeat.write_text('{"ok": true}', encoding="utf-8")
        stale_time = time.time() - 601
        os.utime(heartbeat, (stale_time, stale_time))

        result = self.watch.run_once()

        self.assertTrue(result["stuck"])
        self.assertIn("Active task produced no artifact", result["matched_rules"])
        self.assertIn("Active task produced only liveness noise", result["matched_rules"])
        self.assertIn(task.id, result["affected_task_ids"])

    def test_watch_detects_active_task_that_lost_worker(self) -> None:
        task = self._create_task(status=TaskStatus.ACTIVE)
        self._create_run(task)

        result = self.watch.run_once()

        self.assertTrue(result["stuck"])
        self.assertIn("Active task lost its worker", result["matched_rules"])
        self.assertIn(task.id, result["affected_task_ids"])

    def test_watch_detects_promotion_blocked_on_missing_prerequisite(self) -> None:
        task = self._create_task(status=TaskStatus.COMPLETED, required_artifacts=["plan", "report"])
        run = self._create_run(task, status=RunStatus.COMPLETED, updated_at=datetime.now(UTC) - timedelta(minutes=5))
        self.store.create_artifact(
            Artifact(id=new_id("artifact"), run_id=run.id, kind="plan", path="/tmp/plan.txt", summary="plan")
        )
        self.store.create_promotion(
            PromotionRecord(
                id=new_id("promotion"),
                task_id=task.id,
                run_id=run.id,
                status=PromotionStatus.PENDING,
                summary="pending",
                details={},
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )

        result = self.watch.run_once()

        self.assertTrue(result["stuck"])
        self.assertIn("Promotion is blocked on a missing prerequisite", result["matched_rules"])
        self.assertTrue(result["affected_promotion_ids"])

    def test_watch_counts_same_state_loops_deterministically(self) -> None:
        task = self._create_task(status=TaskStatus.PENDING)
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(minutes=3)).isoformat(), task.id),
            )

        first = self.watch.run_once()
        second = self.watch.run_once()

        self.assertTrue(first["stuck"])
        self.assertTrue(second["stuck"])
        self.assertIn("Task is looping in the same state", second["matched_rules"])


class ControlRuntimeObserverWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace_root = root / "workspace"
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()
        self.control_plane = ControlPlane(self.store)
        self.control_plane.turn_on()
        self.observer = ControlRuntimeObserver(
            self.store,
            self.control_plane,
            FailureClassifier(),
            BreadcrumbWriter(self.store, self.workspace_root),
        )

    def test_failure_event_records_worker_run_and_breadcrumb(self) -> None:
        self.observer.handle({"type": "run_created", "task_id": "task_123", "run_id": "run_123", "attempt": 1})
        self.observer.handle(
            {
                "type": "failure_diagnostic",
                "task_id": "task_123",
                "run_id": "run_123",
                "attempt": 1,
                "run_status": "failed",
                "task_status": "pending",
                "failure_category": "validation_timeout",
                "failure_message": "Worker timed out after validation.",
                "analysis_summary": "Timed out",
            }
        )

        worker_run = self.store.get_control_worker_run("run_123")
        lane = self.store.get_control_lane_state("worker")
        breadcrumbs = self.store.list_control_breadcrumbs(entity_type="task", entity_id="task_123")

        self.assertIsNotNone(worker_run)
        self.assertEqual("timeout", worker_run.classification if worker_run else None)
        self.assertEqual("paused", lane.state.value if lane else None)
        self.assertEqual("timeout", breadcrumbs[0].classification)


class ControlRuntimeObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace_root = root / "workspace"
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()
        self.control_plane = ControlPlane(self.store)
        self.control_plane.turn_on()
        self.observer = ControlRuntimeObserver(
            self.store,
            self.control_plane,
            FailureClassifier(),
            BreadcrumbWriter(self.store, self.workspace_root),
        )

    def test_build_context_wires_post_task_workflow_callback_for_supervisor_path(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "context.db",
            workspace_root=root / "context-workspace",
            log_path=root / "context.log",
            config_file=root / "context-config.json",
        )

        ctx = build_context(config)

        self.assertTrue(hasattr(ctx.engine, "queue"))
        self.assertIsNotNone(ctx.sa_watch.structural_progress_callback)
        self.assertIsNotNone(ctx.sa_watch.restart_stack)
        self.assertIsNotNone(ctx.engine.queue.post_task_callback)
        self.assertIsNotNone(ctx.workflow_data_service)

    def test_task_started_marks_worker_lane_running(self) -> None:
        self.observer.handle({"type": "task_started", "task_id": "task_123"})

        lane = self.store.get_control_lane_state("worker")

        self.assertIsNotNone(lane)
        self.assertEqual("running", lane.state.value if lane else None)

    def test_provider_rate_limit_enters_cooldown(self) -> None:
        self.observer.handle({"type": "run_created", "task_id": "task_123", "run_id": "run_123", "attempt": 1})
        self.observer.handle(
            {
                "type": "failure_diagnostic",
                "task_id": "task_123",
                "run_id": "run_123",
                "attempt": 1,
                "run_status": "failed",
                "task_status": "pending",
                "failure_category": "provider_429",
                "failure_message": "Provider returned 429 rate limit.",
                "analysis_summary": "API rate limit reached.",
            }
        )

        lane = self.store.get_control_lane_state("worker")
        status = self.control_plane.status()

        self.assertIsNotNone(lane)
        self.assertEqual("cooldown", lane.state.value if lane else None)
        self.assertEqual("degraded", status["global_state"])
        self.assertEqual("worker", status["cooldowns"][0]["lane"])

    def test_artifact_contract_failure_keeps_worker_lane_running(self) -> None:
        self.observer.handle({"type": "run_created", "task_id": "task_123", "run_id": "run_123", "attempt": 1})
        self.observer.handle(
            {
                "type": "failure_diagnostic",
                "task_id": "task_123",
                "run_id": "run_123",
                "attempt": 1,
                "run_status": "failed",
                "task_status": "pending",
                "failure_category": "validation_failure",
                "failure_message": "Expected objective_review_packet artifact was not persisted.",
                "analysis_summary": "Run is missing required artifacts.",
                "decision_rationale": "Artifacts were insufficient; retry within bounded task budget.",
                "worker_outcome": "failed",
            }
        )

        worker_run = self.store.get_control_worker_run("run_123")
        lane = self.store.get_control_lane_state("worker")
        status = self.control_plane.status()
        escalations = self.store.list_control_events(event_type="human_escalation_required")

        self.assertEqual("artifact_contract_failure", worker_run.classification if worker_run else None)
        self.assertEqual("running", lane.state.value if lane else None)
        self.assertEqual("healthy", status["global_state"])
        self.assertFalse(escalations)

    def test_no_progress_records_objective_scoped_escalation_without_pausing_worker_lane(self) -> None:
        tasks = TaskService(self.store)
        project = tasks.create_project("progress-project", "progress")
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Progress objective",
            summary="needs promotion progress",
            status=ObjectiveStatus.EXECUTING,
        )
        self.store.create_objective(objective)
        created_tasks = [
            tasks.create_task_with_policy(
                project_id=project.id,
                objective_id=objective.id,
                title=f"Task {index}",
                objective="Do work that does not resolve the objective.",
                priority=100 + index,
                parent_task_id=None,
                source_run_id=None,
                external_ref_type=None,
                external_ref_id=None,
            )
            for index in range(4)
        ]
        for index, task in enumerate(created_tasks[:3]):
            self.observer.handle({"type": "run_created", "task_id": task.id, "run_id": f"run_{index}", "attempt": 1})
            self.store.update_task_status(task.id, TaskStatus.COMPLETED)
            self.observer.handle(
                {
                    "type": "task_finished",
                    "task_id": task.id,
                    "run_id": f"run_{index}",
                    "status": "completed",
                    "run_status": "completed",
                }
            )

        lane = self.store.get_control_lane_state("worker")
        actions = self.store.list_control_recovery_actions()
        escalations = self.store.list_control_events(event_type="human_escalation_required")
        breadcrumbs = self.store.list_control_breadcrumbs(entity_type="objective", entity_id=objective.id)

        self.assertIsNotNone(lane)
        self.assertEqual("running", lane.state.value if lane else None)
        self.assertEqual("escalate", actions[0].action_type)
        self.assertEqual(objective.id, escalations[0].payload.get("objective_id") if escalations else None)
        self.assertTrue(self.control_plane.objective_no_progress_blocked(objective.id))
        self.assertEqual("no_progress", breadcrumbs[0].classification)

    def test_structural_fix_completion_requests_stack_restart(self) -> None:
        observer = ControlRuntimeObserver(
            self.store,
            self.control_plane,
            FailureClassifier(),
            BreadcrumbWriter(self.store, self.workspace_root),
        )
        tasks = TaskService(self.store)
        project = tasks.create_project("restart-project", "restart")
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Restart objective",
            summary="needs restart after structural fix",
            status=ObjectiveStatus.EXECUTING,
        )
        self.store.create_objective(objective)
        structural_task = tasks.create_task_with_policy(
            project_id=project.id,
            objective_id=objective.id,
            title="Structural fix",
            objective="Fix it",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="sa_watch",
            external_ref_id=f"objective:{objective.id}:stale_atomic_generation",
            strategy="sa_structural_fix",
        )
        self.store.update_task_status(structural_task.id, TaskStatus.COMPLETED)

        observer.handle(
            {
                "type": "task_finished",
                "task_id": structural_task.id,
                "run_id": "run_structural",
                "status": "completed",
                "run_status": "completed",
            }
        )

        lane = self.store.get_control_lane_state("worker")
        system = self.store.get_control_system_state()
        self.assertEqual("running", lane.state.value if lane else None)
        self.assertEqual("healthy", system.global_state.value)

    def test_structural_fix_completion_runs_promotion_before_restart(self) -> None:
        observer = ControlRuntimeObserver(
            self.store,
            self.control_plane,
            FailureClassifier(),
            BreadcrumbWriter(self.store, self.workspace_root),
        )
        tasks = TaskService(self.store)
        project = tasks.create_project("restart-project", "restart")
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Restart objective",
            summary="needs restart after structural fix",
            status=ObjectiveStatus.EXECUTING,
        )
        self.store.create_objective(objective)
        structural_task = tasks.create_task_with_policy(
            project_id=project.id,
            objective_id=objective.id,
            title="Structural fix",
            objective="Fix it",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="sa_watch",
            external_ref_id=f"objective:{objective.id}:stale_atomic_generation",
            strategy="sa_structural_fix",
        )
        self.store.update_task_status(structural_task.id, TaskStatus.COMPLETED)

        observer.handle(
            {
                "type": "task_finished",
                "task_id": structural_task.id,
                "run_id": "run_structural",
                "status": "completed",
                "run_status": "completed",
            }
        )

        actions = self.store.list_control_recovery_actions(target_type="system", target_id="system")
        self.assertEqual([], actions)

    def test_non_structural_fix_completion_does_not_run_promotion(self) -> None:
        observer = ControlRuntimeObserver(
            self.store,
            self.control_plane,
            FailureClassifier(),
            BreadcrumbWriter(self.store, self.workspace_root),
        )
        tasks = TaskService(self.store)
        project = tasks.create_project("normal-project", "normal")
        task = tasks.create_task_with_policy(
            project_id=project.id,
            objective_id=None,
            title="Normal task",
            objective="Complete normal work",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
        )
        self.store.update_task_status(task.id, TaskStatus.COMPLETED)

        observer.handle(
            {
                "type": "task_finished",
                "task_id": task.id,
                "run_id": "run_normal",
                "status": "completed",
                "run_status": "completed",
            }
        )

        system = self.store.get_control_system_state()
        self.assertEqual("healthy", system.global_state.value)


class StructuralFixPromotionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(self.root / "harness.db")
        self.store.initialize()
        self.workspace_root = self.root / "workspace"
        self.breadcrumb_writer = BreadcrumbWriter(self.store, self.workspace_root)
        self.project = Project(id=new_id("project"), name="repo-promotion", description="repo promotion")
        self.store.create_project(self.project)
        self.objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Objective",
            summary="summary",
            status=ObjectiveStatus.EXECUTING,
        )
        self.store.create_objective(self.objective)

    def _create_structural_task(self) -> object:
        task = TaskService(self.store).create_task_with_policy(
            project_id=self.project.id,
            objective_id=self.objective.id,
            title="Structural fix",
            objective="Fix it",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="sa_watch",
            external_ref_id=f"objective:{self.objective.id}:stale_atomic_generation",
            strategy="sa_structural_fix",
        )
        self.store.update_task_status(task.id, TaskStatus.COMPLETED)
        return task

    def _record_workspace(self, run_id: str, repo_root: Path, *, workspace_mode: str = "shared_repo") -> None:
        self.store.create_event(
            ControlEvent(
                id=new_id("event"),
                entity_type="run",
                entity_id=run_id,
                event_type="project_workspace_prepared",
                producer="test",
                payload={
                    "project_root": str(repo_root),
                    "workspace_mode": workspace_mode,
                    "source_repo_root": str(repo_root),
                },
                idempotency_key=new_id("event_key"),
            )
        )

    def _write_report(self, run_id: str, changed_files: list[str]) -> None:
        run_dir = self.root / "workspace" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.json").write_text(json.dumps({"changed_files": changed_files}), encoding="utf-8")

    def test_failed_ci_blocks_push_and_records_artifact(self) -> None:
        task = self._create_structural_task()
        repo_root = self.root / "repo"
        repo_root.mkdir()
        self._record_workspace("run_structural", repo_root)
        self._write_report("run_structural", ["src/fix.py"])
        announcements: list[str] = []

        class _RepoPromotions:
            def run_local_ci(self, workspace_root: Path) -> LocalCIResult:
                return LocalCIResult(
                    passed=False,
                    failed_stage="fast",
                    command_summary=("make test-fast",),
                    logs={"fast_tests": str(workspace_root / "fast.log")},
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    summary="Local CI parity failed during the fast stage.",
                )

        service = StructuralFixPromotionService(  # type: ignore[arg-type]
            self.store,
            self.breadcrumb_writer,
            _RepoPromotions(),
            announce=announcements.append,
        )

        result = service.promote_completed_structural_fix(task, "run_structural")

        self.assertEqual("blocked", result["status"])
        self.assertEqual("ci_failed", result["reason"])
        updated = self.store.get_task(task.id)
        self.assertFalse(bool(updated.external_ref_metadata["sa_watch_promotion"]["ci_passed"]))
        self.assertEqual(["make test-fast"], updated.external_ref_metadata["sa_watch_promotion"]["command_summary"])
        actions = self.store.list_control_recovery_actions(target_type="task", target_id=task.id)
        self.assertEqual("observe", actions[0].action_type)
        breadcrumbs = self.store.list_control_breadcrumbs(entity_type="task", entity_id=task.id)
        self.assertEqual("ci_failed", breadcrumbs[0].classification)
        self.assertEqual(
            [
                "running full local CI before pushing a recovery fix",
                "local CI failed during the fast stage; recovery changes were kept local",
            ],
            announcements,
        )

    def test_passed_ci_commits_and_pushes_to_main(self) -> None:
        task = self._create_structural_task()
        repo_root = self.root / "repo"
        repo_root.mkdir()
        self._record_workspace("run_structural", repo_root)
        self._write_report("run_structural", ["src/fix.py"])
        git_calls: list[tuple[str, ...]] = []
        announcements: list[str] = []

        class _RepoPromotions:
            def run_local_ci(self, workspace_root: Path) -> LocalCIResult:
                return LocalCIResult(
                    passed=True,
                    failed_stage="unknown",
                    command_summary=("make test-fast", "make test-temporal"),
                    logs={"fast_tests": str(workspace_root / "fast.log")},
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    summary="Local CI parity passed.",
                )

            def _git_output(self, workspace_root: Path, *args: str) -> str:
                git_calls.append(("git_output", *args))
                if args == ("status", "--porcelain"):
                    return " M src/fix.py\n"
                if args == ("diff", "--cached", "--name-only"):
                    return "src/fix.py\n"
                if args == ("rev-parse", "HEAD"):
                    return "abc123\n"
                return ""

            def _git(self, workspace_root: Path, *args: str) -> None:
                git_calls.append(("git", *args))

            def _verify_remote_sha(self, workspace_root: Path, base_branch: str, expected_sha: str) -> str:
                git_calls.append(("verify", base_branch, expected_sha))
                return expected_sha

        with patch("accruvia_harness.services.structural_fix_promotion_service.subprocess.run") as subprocess_run:
            subprocess_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            service = StructuralFixPromotionService(  # type: ignore[arg-type]
                self.store,
                self.breadcrumb_writer,
                _RepoPromotions(),
                announce=announcements.append,
            )
            result = service.promote_completed_structural_fix(task, "run_structural")

        self.assertEqual("pushed", result["status"])
        self.assertEqual("abc123", result["commit_sha"])
        updated = self.store.get_task(task.id)
        self.assertEqual("pushed", updated.external_ref_metadata["sa_watch_promotion"]["push_status"])
        self.assertEqual(
            ["make test-fast", "make test-temporal"],
            updated.external_ref_metadata["sa_watch_promotion"]["command_summary"],
        )
        self.assertIn(("git", "commit", "-m", f"sa-watch: unblock objective {self.objective.id}"), git_calls)
        self.assertIn(("git", "push", "origin", "HEAD:main"), git_calls)
        self.assertEqual(
            [
                "running full local CI before pushing a recovery fix",
                "local CI passed; pushing the recovery fix to main",
                "recovery fix pushed to main as abc123",
            ],
            announcements,
        )

    def test_control_loop_clears_stale_restart_request_during_startup_preflight(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "loop.db",
            workspace_root=root / "loop-workspace",
            log_path=root / "loop.log",
            config_file=root / "loop-config.json",
        )
        ctx = build_context(config)
        ctx.control_plane.turn_on()
        ctx.store.create_control_recovery_action(
            ControlRecoveryAction(
                id=new_id("recovery"),
                action_type="observe",
                target_type="system",
                target_id="system",
                reason="baseline",
                result="recorded",
            )
        )
        from accruvia_harness.commands.common import record_stack_restart_request

        record_stack_restart_request(config, {"reason": "sa_structural_fix_completed", "task_id": "task_123"})

        args = Namespace(
            command="control-loop",
            api_url=None,
            stalled_objective_hours=6.0,
            no_freeze_on_stall=False,
            interval_seconds=0.1,
            max_iterations=1,
        )

        with (
            patch("accruvia_harness.commands.control.restart_api_process", return_value={"pid": 1}) as restart_api,
            patch("accruvia_harness.commands.control.restart_harness_process", return_value={"pid": 2}) as restart_harness,
            patch("accruvia_harness.commands.control.restart_control_loop_process", return_value={"pid": 3}) as restart_loop,
        ):
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        restart_api.assert_not_called()
        restart_harness.assert_not_called()
        restart_loop.assert_not_called()
        self.assertIsNone(read_stack_restart_request(config))
        actions = ctx.store.list_control_recovery_actions(target_type="system", target_id="system")
        self.assertEqual("observe", actions[0].action_type)

    def test_control_loop_runs_sa_watch_and_restarts_when_stuck_detected(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "loop-stuck.db",
            workspace_root=root / "loop-stuck-workspace",
            log_path=root / "loop-stuck.log",
            config_file=root / "loop-stuck-config.json",
        )
        ctx = build_context(config)
        ctx.control_plane.turn_on()
        project = Project(id=new_id("project"), name="loop-project", description="loop")
        ctx.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Pending forever",
            objective="Pending forever",
            status=TaskStatus.PENDING,
        )
        ctx.store.create_task(task)
        with ctx.store.connect() as connection:
            connection.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(minutes=3)).isoformat(), task.id),
            )

        args = Namespace(
            command="control-loop",
            api_url=None,
            stalled_objective_hours=6.0,
            no_freeze_on_stall=False,
            interval_seconds=0.1,
            max_iterations=1,
        )

        with (
            patch.object(ctx.sa_watch, "run_once", return_value={"decision": {"action": "repair_workflow_state"}}) as sa_watch_run,
            patch("accruvia_harness.commands.control.restart_harness_process", return_value={"pid": 2}) as restart_harness,
            patch("accruvia_harness.commands.control.restart_control_loop_process", return_value={"pid": 3}) as restart_loop,
        ):
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        sa_watch_run.assert_called_once()
        restart_harness.assert_called_once_with(config, force=True)
        restart_loop.assert_called_once()

    def test_control_loop_runs_startup_preflight_before_evaluating_stuck(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "loop-preflight.db",
            workspace_root=root / "loop-preflight-workspace",
            log_path=root / "loop-preflight.log",
            config_file=root / "loop-preflight-config.json",
        )
        ctx = build_context(config)
        ctx.control_plane.turn_on()
        args = Namespace(
            command="control-loop",
            api_url=None,
            stalled_objective_hours=6.0,
            no_freeze_on_stall=False,
            interval_seconds=0.1,
            max_iterations=1,
        )

        with (
            patch("accruvia_harness.commands.control.startup_preflight") as preflight,
            patch.object(ctx.control_watch, "run_once", return_value={"stuck": False}),
            patch("accruvia_harness.commands.control.emit"),
        ):
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        preflight.assert_called_once_with(config, ctx.store)

    def test_supervise_does_not_autostart_sa_watch(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "supervise-no-sa-watch.db",
            workspace_root=root / "supervise-no-sa-watch-workspace",
            log_path=root / "supervise-no-sa-watch.log",
            config_file=root / "supervise-no-sa-watch-config.json",
        )
        store = SQLiteHarnessStore(config.db_path)
        store.initialize()
        control_plane = ControlPlane(store)
        control_plane.turn_on()

        class _Engine:
            def supervise(self, **kwargs):
                return _FakeSuperviseResult(processed_count=0, processed_task_ids=[], exit_reason="idle")

        ctx = SimpleNamespace(
            config=config,
            store=store,
            engine=_Engine(),
            control_plane=control_plane,
            control_runtime=SimpleNamespace(handle=lambda event: None),
            control_watch=SimpleNamespace(observe=lambda event, api_url=None: None),
        )
        args = Namespace(
            command="run-harness",
            json=False,
            project_id=None,
            worker_id="harness",
            lease_seconds=300,
            watch=False,
            idle_sleep_seconds=0.0,
            max_idle_cycles=None,
            max_iterations=1,
            heartbeat_project_ids=[],
            heartbeat_interval_seconds=None,
            heartbeat_all_projects=False,
            review_check_enabled=False,
            review_check_interval_seconds=None,
        )

        with (
            patch("accruvia_harness.commands.core.print") as print_mock,
        ):
            handled = handle_core_command(args, ctx)

        self.assertTrue(handled)
        printed = "\n".join(call.args[0] for call in print_mock.call_args_list if call.args)
        self.assertNotIn("sa-watch started", printed)
        self.assertNotEqual("off", control_plane.status()["global_state"])
        self.assertTrue(control_plane.status()["master_switch"])

    def test_supervise_turns_control_plane_on_before_running(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "supervise-turn-on.db",
            workspace_root=root / "supervise-turn-on-workspace",
            log_path=root / "supervise-turn-on.log",
            config_file=root / "supervise-turn-on-config.json",
        )
        store = SQLiteHarnessStore(config.db_path)
        store.initialize()
        control_plane = ControlPlane(store)

        class _Engine:
            def supervise(self, **kwargs):
                return _FakeSuperviseResult(processed_count=0, processed_task_ids=[], exit_reason="idle")

        ctx = SimpleNamespace(
            config=config,
            store=store,
            engine=_Engine(),
            control_plane=control_plane,
            control_runtime=SimpleNamespace(handle=lambda event: None),
            control_watch=SimpleNamespace(observe=lambda event, api_url=None: None),
        )
        args = Namespace(
            command="run-harness",
            json=True,
            project_id=None,
            worker_id="harness",
            lease_seconds=300,
            watch=False,
            idle_sleep_seconds=0.0,
            max_idle_cycles=None,
            max_iterations=1,
            heartbeat_project_ids=[],
            heartbeat_interval_seconds=None,
            heartbeat_all_projects=False,
            review_check_enabled=False,
            review_check_interval_seconds=None,
        )

        with patch("accruvia_harness.commands.core.emit"):
            handled = handle_core_command(args, ctx)

        self.assertTrue(handled)
        self.assertNotEqual("off", control_plane.status()["global_state"])
        self.assertTrue(control_plane.status()["master_switch"])

    def test_supervise_runs_startup_preflight_before_turning_on_control_plane(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "supervise-preflight.db",
            workspace_root=root / "supervise-preflight-workspace",
            log_path=root / "supervise-preflight.log",
            config_file=root / "supervise-preflight-config.json",
        )
        store = SQLiteHarnessStore(config.db_path)
        store.initialize()
        control_plane = ControlPlane(store)

        class _Engine:
            def supervise(self, **kwargs):
                return _FakeSuperviseResult(processed_count=0, processed_task_ids=[], exit_reason="idle")

        ctx = SimpleNamespace(
            config=config,
            store=store,
            engine=_Engine(),
            control_plane=control_plane,
            control_runtime=SimpleNamespace(handle=lambda event: None),
            control_watch=SimpleNamespace(observe=lambda event, api_url=None: None),
        )
        args = Namespace(
            command="run-harness",
            json=True,
            project_id=None,
            worker_id="harness",
            lease_seconds=300,
            watch=False,
            idle_sleep_seconds=0.0,
            max_idle_cycles=None,
            max_iterations=1,
            heartbeat_project_ids=[],
            heartbeat_interval_seconds=None,
            heartbeat_all_projects=False,
            review_check_enabled=False,
            review_check_interval_seconds=None,
        )

        with (
            patch("accruvia_harness.commands.core.startup_preflight") as preflight,
            patch("accruvia_harness.commands.core.emit"),
        ):
            handled = handle_core_command(args, ctx)

        self.assertTrue(handled)
        preflight.assert_called_once_with(config, store)

    def test_supervise_interrupt_turns_system_off_without_stopping_sa_watch(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "supervise-interrupt.db",
            workspace_root=root / "supervise-interrupt-workspace",
            log_path=root / "supervise-interrupt.log",
            config_file=root / "supervise-interrupt-config.json",
        )
        store = SQLiteHarnessStore(config.db_path)
        store.initialize()
        control_plane = ControlPlane(store)
        control_plane.turn_on()

        class _Engine:
            def supervise(self, **kwargs):
                raise KeyboardInterrupt

        ctx = SimpleNamespace(
            config=config,
            store=store,
            engine=_Engine(),
            control_plane=control_plane,
            control_runtime=SimpleNamespace(handle=lambda event: None),
            control_watch=SimpleNamespace(observe=lambda event, api_url=None: None),
        )
        args = Namespace(
            command="run-harness",
            json=True,
            project_id=None,
            worker_id="harness",
            lease_seconds=300,
            watch=True,
            idle_sleep_seconds=0.0,
            max_idle_cycles=None,
            max_iterations=1,
            heartbeat_project_ids=[],
            heartbeat_interval_seconds=None,
            heartbeat_all_projects=False,
            review_check_enabled=False,
            review_check_interval_seconds=None,
        )

        with (
            patch("accruvia_harness.commands.core.stop_ui_process") as stop_ui,
        ):
            with self.assertRaises(KeyboardInterrupt):
                handle_core_command(args, ctx)

        stop_ui.assert_called_once_with(config)
        self.assertEqual("off", control_plane.status()["global_state"])


class ControlPlaneSplitTests(unittest.TestCase):
    """Tests verifying the control-plane split: supervise runs work only,
    control-loop is the single recovery authority."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_control_loop_detects_stalled_objective_as_stuck(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "stalled-obj.db",
            workspace_root=root / "stalled-obj-workspace",
            log_path=root / "stalled-obj.log",
            config_file=root / "stalled-obj-config.json",
        )
        ctx = build_context(config)
        ctx.control_plane.turn_on()
        project = Project(id=new_id("project"), name="stalled-proj", description="stalled")
        ctx.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Stalled objective",
            summary="test",
            status=ObjectiveStatus.OPEN,
        )
        ctx.store.create_objective(objective)
        ctx.store.create_control_event(ControlEvent(
            id=new_id("event"),
            event_type="objective_stalled",
            entity_type="objective",
            entity_id=objective.id,
            producer="test",
            payload={"objective_id": objective.id},
            idempotency_key=new_id("idem"),
        ))
        args = Namespace(
            command="control-loop",
            api_url=None,
            stalled_objective_hours=0.0,
            no_freeze_on_stall=False,
            interval_seconds=0.1,
            max_iterations=1,
        )
        with (
            patch.object(ctx.sa_watch, "run_once", return_value={"decision": {"action": "none"}}) as sa_watch_run,
            patch("accruvia_harness.commands.control.restart_harness_process", return_value={"pid": 2}),
            patch("accruvia_harness.commands.control.restart_control_loop_process", return_value={"pid": 3}),
        ):
            handled = handle_control_command(args, ctx)
        self.assertTrue(handled)
        sa_watch_run.assert_called_once()

    def test_startup_preflight_clears_stale_runtime_state(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "preflight-stale.db",
            workspace_root=root / "preflight-stale-workspace",
            log_path=root / "preflight-stale.log",
            config_file=root / "preflight-stale-config.json",
        )
        store = SQLiteHarnessStore(config.db_path)
        store.initialize()
        sa_watch_path = sa_watch_runtime_state_path(config)
        sa_watch_path.parent.mkdir(parents=True, exist_ok=True)
        sa_watch_path.write_text(json.dumps({"pid": 99999, "heartbeat_at": 0}), encoding="utf-8")
        result = startup_preflight(config, store)
        self.assertTrue(result["stale_sa_watch_runtime_cleared"])
        self.assertFalse(sa_watch_path.exists())

    def test_sa_watch_status_requires_fresh_heartbeat(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "status-hb.db",
            workspace_root=root / "status-hb-workspace",
            log_path=root / "status-hb.log",
            config_file=root / "status-hb-config.json",
        )
        ctx = build_context(config)
        ctx.control_plane.turn_on()
        runtime_path = sa_watch_runtime_state_path(config)
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(json.dumps({
            "pid": os.getpid(),
            "heartbeat_at": time.time() - 300,
            "mode": "active",
            "interval_seconds": 60,
        }), encoding="utf-8")
        args = Namespace(command="sa-watch-status")
        captured = {}
        with patch("accruvia_harness.commands.control.emit", side_effect=lambda v: captured.update(v)):
            handle_control_command(args, ctx)
        self.assertTrue(captured.get("running"))
        self.assertFalse(captured.get("heartbeat_fresh"))
        self.assertFalse(captured.get("healthy"))

    def test_no_duplicate_recovery_processes_on_repeated_stuck(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "no-dup.db",
            workspace_root=root / "no-dup-workspace",
            log_path=root / "no-dup.log",
            config_file=root / "no-dup-config.json",
        )
        ctx = build_context(config)
        ctx.control_plane.turn_on()
        project = Project(id=new_id("project"), name="dup-proj", description="dup")
        ctx.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Stuck task",
            objective="stuck",
            status=TaskStatus.PENDING,
        )
        ctx.store.create_task(task)
        with ctx.store.connect() as connection:
            connection.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(minutes=3)).isoformat(), task.id),
            )
        args = Namespace(
            command="control-loop",
            api_url=None,
            stalled_objective_hours=6.0,
            no_freeze_on_stall=False,
            interval_seconds=0.1,
            max_iterations=1,
        )
        sa_watch_call_count = 0

        def _sa_watch_run_once():
            nonlocal sa_watch_call_count
            sa_watch_call_count += 1
            return {"decision": {"action": "none"}}

        with (
            patch.object(ctx.sa_watch, "run_once", side_effect=_sa_watch_run_once),
            patch("accruvia_harness.commands.control.restart_harness_process", return_value={"pid": 2}),
            patch("accruvia_harness.commands.control.restart_control_loop_process", return_value={"pid": 3}),
        ):
            handle_control_command(args, ctx)
        self.assertEqual(1, sa_watch_call_count)


class SAWatchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace_root = root / "workspace"
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()
        self.control_plane = ControlPlane(self.store)
        self.control_plane.turn_on()

    def test_prompt_includes_fixed_action_contract(self) -> None:
        executor = FakeExecutor('{"action":"record_escalation","reason":"Ambiguous repeated timeout.","confidence":0.8,"target_lane":"worker","escalate":true}')
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter("codex", {"codex": executor}),
            self.workspace_root,
            interval_seconds=0,
        )
        self.control_plane.mark_degraded("timeout")

        service.run_once()

        self.assertTrue(executor.prompts)
        prompt = executor.prompts[0]
        self.assertIn("You are sa-watch for the Accruvia harness control plane.", prompt)
        self.assertIn('"repair_harness"', prompt)
        self.assertIn('"freeze_system"', prompt)
        self.assertIn('"resume_worker"', prompt)
        self.assertIn('"restart_stack"', prompt)
        self.assertIn('"repair_workflow_state"', prompt)
        self.assertIn("keep work moving", prompt)
        self.assertIn("Output JSON only.", prompt)

    def test_sa_watch_records_escalation_for_objective_stall_when_model_declines_intervention(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Stalled objective",
            summary="stalled",
            status=ObjectiveStatus.EXECUTING,
        )
        self.store.create_objective(objective)
        self.store.create_control_event(
            ControlEvent(
                id=new_id("control_event"),
                event_type="objective_stalled",
                entity_type="objective",
                entity_id=objective.id,
                producer="test",
                payload={"objective_id": objective.id},
                idempotency_key=new_id("event_key"),
            )
        )
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        '{"action":"record_escalation","reason":"Repeated timeout after deterministic retries exhausted.","confidence":0.91,"target_lane":"worker","escalate":true}'
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
            repair_runner=lambda task, run, repo_root: SAWatchRepairResult(
                status="failed",
                run_id=run.id,
                run_dir=self.workspace_root / "control" / "sa_watch_repairs" / run.id,
                summary="not needed for signal preference test",
                changed_files=[],
                validation={},
                diagnostics={},
            ),
        )
        self.control_plane.mark_degraded("timeout")

        result = service.observe({"type": "sleeping"})

        self.assertIsNotNone(result)
        actions = self.store.list_control_recovery_actions()
        self.assertEqual("escalate", actions[0].action_type)
        created = [task for task in self.store.list_tasks(project.id) if task.external_ref_type == "sa_watch"]
        self.assertEqual(0, len(created))

    def test_sa_watch_records_escalation_when_model_only_escalates_repeated_failure(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Broken objective",
            summary="broken",
            status=ObjectiveStatus.PAUSED,
        )
        self.store.create_objective(objective)
        failing_task = TaskService(self.store).create_task_with_policy(
            project_id=project.id,
            objective_id=objective.id,
            title="Retrying task",
            objective="Keep failing the same way.",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="default",
            max_attempts=4,
        )
        self.store.upsert_control_worker_run(ControlWorkerRun(id="run_1", task_id=failing_task.id, status="failed", classification="timeout"))
        self.store.upsert_control_worker_run(ControlWorkerRun(id="run_2", task_id=failing_task.id, status="failed", classification="timeout"))
        self.control_plane.pause_lane("worker", reason="timeout")
        self.control_plane.mark_degraded("timeout")
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        '{"action":"record_escalation","reason":"Repeated timeout but structural cause not confidently proven.","confidence":0.61,"target_lane":"worker","target_task_id":"'
                        + failing_task.id
                        + '","escalate":true}'
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
        )

        result = service.observe({"type": "sleeping"})

        self.assertIsNotNone(result)
        created = self.store.get_task_by_external_ref("sa_watch", f"{failing_task.id}:timeout")
        self.assertIsNone(created)
        actions = self.store.list_control_recovery_actions()
        self.assertEqual("escalate", actions[0].action_type)

    def test_sa_watch_ignores_repeated_artifact_contract_failures(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Evidence objective",
            summary="needs evidence artifact",
            status=ObjectiveStatus.PAUSED,
        )
        self.store.create_objective(objective)
        task = TaskService(self.store).create_task_with_policy(
            project_id=project.id,
            objective_id=objective.id,
            title="Retrying evidence task",
            objective="Keep producing the required packet artifact.",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="default",
            max_attempts=4,
        )
        self.store.upsert_control_worker_run(
            ControlWorkerRun(id="run_1", task_id=task.id, status="failed", classification="artifact_contract_failure")
        )
        self.store.upsert_control_worker_run(
            ControlWorkerRun(id="run_2", task_id=task.id, status="failed", classification="artifact_contract_failure")
        )
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        '{"action":"record_escalation","reason":"No intervention needed.","confidence":0.8,"target_lane":"worker","escalate":false}'
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
        )

        packet = service._build_packet()  # type: ignore[attr-defined]
        repeated = [signal for signal in packet["continuity_signals"] if signal.get("kind") == "repeated_failure"]

        self.assertEqual([], repeated)

    def test_sa_watch_detects_low_value_churn_from_repeated_insufficient_artifacts(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Churning objective",
            summary="retries are not shrinking the backlog",
            status=ObjectiveStatus.EXECUTING,
        )
        self.store.create_objective(objective)
        tasks = [
            TaskService(self.store).create_task_with_policy(
                project_id=project.id,
                objective_id=objective.id,
                title=f"Retrying task {index}",
                objective="Keep trying the same low-value artifact-producing path.",
                priority=100,
                parent_task_id=None,
                source_run_id=None,
                external_ref_type=None,
                external_ref_id=None,
                strategy="default",
                max_attempts=4,
            )
            for index in range(3)
        ]
        for index, task in enumerate(tasks):
            run = Run(
                id=f"run_{index}",
                task_id=task.id,
                status=RunStatus.FAILED,
                attempt=1,
                summary="Artifacts were insufficient; retry within bounded task budget.",
            )
            self.store.create_run(run)
            self.store.upsert_control_worker_run(
                ControlWorkerRun(
                    id=run.id,
                    task_id=task.id,
                    objective_id=objective.id,
                    status="failed",
                    classification="artifact_contract_failure",
                )
            )
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        '{"action":"record_escalation","reason":"Repeated insufficient-artifact retries are not reducing backlog.","confidence":0.9,"target_lane":"worker","escalate":true}'
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
        )

        packet = service._build_packet()  # type: ignore[attr-defined]
        churn = [signal for signal in packet["continuity_signals"] if signal.get("kind") == "low_value_churn"]

        self.assertEqual(1, len(churn))
        self.assertEqual(objective.id, churn[0]["objective_id"])
        self.assertEqual(3, churn[0]["count"])

    def test_sa_watch_packet_uses_local_time_context(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        task = TaskService(self.store).create_task_with_policy(
            project_id=project.id,
            objective_id=None,
            title="Time task",
            objective="time",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="default",
        )
        self.store.upsert_control_worker_run(
            ControlWorkerRun(id="run_time", task_id=task.id, status="started", classification=None)
        )
        self.store.create_control_event(
            ControlEvent(
                id=new_id("control_event"),
                event_type="harness_up",
                entity_type="lane",
                entity_id="harness",
                producer="test",
                payload={"supervisor_count": 1},
                idempotency_key=new_id("event_key"),
            )
        )
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter("codex", {"codex": FakeExecutor('{"action":"none","reason":"noop","confidence":0.5,"target_lane":null,"target_task_id":null,"task_title":null,"task_objective":null,"escalate":false}')}),
            self.workspace_root,
            interval_seconds=0,
        )

        packet = service._build_packet()  # type: ignore[attr-defined]

        self.assertEqual("All timestamps in this packet are local time.", packet["time_context"]["note"])
        self.assertIn(" ", packet["time_context"]["now_local"])
        self.assertNotIn("+00:00", packet["recent_events"][0]["created_at"])

    def test_sa_watch_keeps_lane_paused_when_model_response_is_unusable(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Stalled objective",
            summary="stalled",
            status=ObjectiveStatus.PLANNING,
        )
        self.store.create_objective(objective)
        self.store.create_control_event(
            ControlEvent(
                id=new_id("control_event"),
                event_type="objective_stalled",
                entity_type="objective",
                entity_id=objective.id,
                producer="test",
                payload={"objective_id": objective.id},
                idempotency_key=new_id("event_key"),
            )
        )
        self.control_plane.pause_lane("worker", reason="no_progress")
        self.control_plane.mark_degraded("no_progress")
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        '{"action":"record_escalation","reason":"","confidence":0.0,"target_lane":"worker","escalate":false}'
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
        )

        result = service.observe({"type": "sleeping"})

        self.assertIsNotNone(result)
        self.assertEqual("model_response_unusable", result["decision"]["action"])
        worker_lane = self.store.get_control_lane_state("worker")
        self.assertEqual("paused", worker_lane.state.value if worker_lane else None)
        actions = self.store.list_control_recovery_actions(target_type="system", target_id="system")
        self.assertEqual("model_response_unusable", actions[0].action_type)

    def test_sa_watch_uses_model_action_without_structural_task_short_circuit(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Stalled objective",
            summary="stalled",
            status=ObjectiveStatus.PLANNING,
        )
        self.store.create_objective(objective)
        structural_task = TaskService(self.store).create_task_with_policy(
            project_id=project.id,
            objective_id=objective.id,
            title="Unblock stalled objective workflow",
            objective="Make the objective advance again.",
            priority=150,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="sa_watch",
            external_ref_id=f"objective:{objective.id}:objective_stalled",
            strategy="sa_structural_fix",
        )
        self.store.update_task_status(structural_task.id, TaskStatus.ACTIVE)
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        '{"action":"restart_stack","reason":"bad decision that should not be used","confidence":1.0,"target_lane":"worker","escalate":false}'
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
        )

        result = service.observe({"type": "sleeping"})

        self.assertIsNotNone(result)
        self.assertEqual("restart_stack", result["decision"]["action"])
        self.assertEqual("stack_restart_requested", result["effects"][0]["kind"])

    def test_sa_watch_loop_prints_generic_observation(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "sa-watch-active-recovery.db",
            workspace_root=root / "sa-watch-active-recovery-workspace",
            log_path=root / "sa-watch-active-recovery.log",
            config_file=root / "sa-watch-active-recovery-config.json",
        )
        active_store = SQLiteHarnessStore(config.db_path)
        active_store.initialize()
        active_control_plane = ControlPlane(active_store)
        active_control_plane.turn_on()
        supervisor_dir = root / "supervisors"
        supervisor_dir.mkdir(parents=True, exist_ok=True)
        (supervisor_dir / "active_recovery_supervisor.json").write_text(
            json.dumps({"pid": os.getpid(), "worker_id": "supervisor"}),
            encoding="utf-8",
        )
        active_control_watch = ControlWatchService(
            active_store,
            active_control_plane,
            FailureClassifier(),
            BreadcrumbWriter(active_store, config.workspace_root),
            supervisor_control_dir=supervisor_dir,
        )
        ctx = SimpleNamespace(
            config=config,
            store=active_store,
            control_plane=active_control_plane,
            control_watch=active_control_watch,
            sa_watch=SimpleNamespace(
                run_once=lambda: {
                    "decision": {"action": "none", "reason": "structural_fix_in_progress"},
                    "packet": {"continuity_signals": []},
                    "effects": [{"kind": "observed", "reason": "structural_fix_in_progress"}],
                }
            ),
        )
        args = Namespace(command="sa-watch-loop", interval_seconds=0.0, max_iterations=1)

        with (
            patch("accruvia_harness.commands.control.emit"),
            patch("accruvia_harness.commands.control.print") as print_mock,
        ):
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        printed = "\n".join(call.args[0] for call in print_mock.call_args_list if call.args)
        self.assertIn("decision: observe only; signals: none; reason: structural_fix_in_progress", printed)
        self.assertIn("observed only; no code/workflow change made; reason=structural_fix_in_progress", printed)

    def test_sa_watch_records_failed_direct_repair_without_resuming_worker_lane(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Broken objective",
            summary="broken",
            status=ObjectiveStatus.PAUSED,
        )
        self.store.create_objective(objective)
        failing_task = TaskService(self.store).create_task_with_policy(
            project_id=project.id,
            objective_id=objective.id,
            title="Retrying task",
            objective="Keep failing the same way.",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="default",
            max_attempts=4,
        )
        self.store.upsert_control_worker_run(
            ControlWorkerRun(id="run_1", task_id=failing_task.id, status="failed", classification="timeout")
        )
        self.store.upsert_control_worker_run(
            ControlWorkerRun(id="run_2", task_id=failing_task.id, status="failed", classification="timeout")
        )
        self.control_plane.pause_lane("worker", reason="timeout")
        self.control_plane.mark_degraded("timeout")
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        json.dumps(
                            {
                                "action": "repair_harness",
                                "reason": "Repeated timeout indicates the validation path needs a structural fix.",
                                "confidence": 0.92,
                                "target_lane": "worker",
                                "target_task_id": failing_task.id,
                                "task_title": "Prevent recurring validation timeout in worker path",
                                "task_objective": "Make a real code change that prevents the repeated validation timeout and prove it with tests.",
                                "escalate": False,
                            }
                        )
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
            repair_runner=lambda task, run, repo_root: SAWatchRepairResult(
                status="failed",
                run_id=run.id,
                run_dir=self.workspace_root / "control" / "sa_watch_repairs" / run.id,
                summary="validation still failing",
                changed_files=["src/accruvia_harness/control_runtime.py"],
                validation={"compile_check": {"ok": False}, "test_check": {"ok": False}},
                diagnostics={"failure_message": "validation still failing"},
            ),
        )

        result = service.observe({"type": "sleeping"})

        self.assertIsNotNone(result)
        lane = self.store.get_control_lane_state("worker")
        self.assertEqual("paused", lane.state.value if lane else None)
        status = self.store.get_control_system_state()
        self.assertEqual("degraded", status.global_state.value)
        self.assertIsNone(self.store.get_task_by_external_ref("sa_watch", f"{failing_task.id}:timeout"))
        repair_records = self.store.list_context_records(objective_id=objective.id, record_type="sa_watch_repair")
        self.assertEqual(1, len(repair_records))
        repair_tasks = [task for task in self.store.list_tasks(project.id) if task.strategy == "sa_watch_direct_repair"]
        self.assertEqual(1, len(repair_tasks))
        self.assertEqual(TaskStatus.FAILED, repair_tasks[0].status)
        repair_runs = self.store.list_runs(repair_tasks[0].id)
        self.assertEqual(1, len(repair_runs))
        self.assertEqual(RunStatus.FAILED, repair_runs[0].status)

    def test_sa_watch_can_repair_obsolete_workflow_state_directly(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Blocked by obsolete structural fix",
            summary="stalled",
            status=ObjectiveStatus.PAUSED,
        )
        self.store.create_objective(objective)
        legacy_task = TaskService(self.store).create_task_with_policy(
            project_id=project.id,
            objective_id=objective.id,
            title="Legacy sa-watch structural fix",
            objective="Old obsolete structural-fix task.",
            priority=150,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="sa_watch",
            external_ref_id=f"objective:{objective.id}:objective_stalled",
            strategy="sa_structural_fix",
            max_attempts=1,
        )
        self.store.update_task_status(legacy_task.id, TaskStatus.FAILED)
        restarted: list[dict[str, object]] = []
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        json.dumps(
                            {
                                "action": "repair_workflow_state",
                                "reason": "The objective is pinned by obsolete legacy sa-watch recovery state.",
                                "confidence": 0.93,
                                "target_lane": "worker",
                                "escalate": False,
                            }
                        )
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
            restart_stack=lambda payload: restarted.append(payload) or self.control_plane.status(),
        )
        self.control_plane.mark_degraded("objective_stalled")

        result = service.observe({"type": "sleeping"})

        self.assertIsNotNone(result)
        self.assertEqual("repair_workflow_state", result["decision"]["action"])
        updated_task = self.store.get_task(legacy_task.id)
        self.assertIsNotNone(updated_task)
        metadata = updated_task.external_ref_metadata if updated_task is not None else {}
        self.assertEqual("ignore_obsolete", metadata["workflow_state_disposition"]["kind"])
        self.assertEqual("waive_obsolete", metadata["failed_task_disposition"]["kind"])
        objective_after = self.store.get_objective(objective.id)
        self.assertEqual(ObjectiveStatus.PLANNING, objective_after.status if objective_after else None)
        repair_records = self.store.list_context_records(objective_id=objective.id, record_type="sa_watch_workflow_state_repair")
        self.assertEqual(1, len(repair_records))
        self.assertEqual(1, len(restarted))

    def test_sa_watch_can_resume_worker_lane_to_restore_continuity(self) -> None:
        project = Project(id=new_id("project"), name="resume-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Paused objective",
            summary="work should continue",
            status=ObjectiveStatus.EXECUTING,
        )
        self.store.create_objective(objective)
        TaskService(self.store).create_task_with_policy(
            project_id=project.id,
            objective_id=objective.id,
            title="Resume normal work",
            objective="Continue the objective after the pause is cleared.",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
        )
        self.control_plane.pause_lane("worker", reason="operator_pause")
        self.control_plane.mark_degraded("operator_pause")
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        json.dumps(
                            {
                                "action": "resume_worker",
                                "reason": "Worker is paused even though runnable work is queued and no structural repair is active.",
                                "confidence": 0.89,
                                "target_lane": "worker",
                                "escalate": False,
                            }
                        )
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
        )

        result = service.observe({"type": "sleeping"})

        self.assertIsNotNone(result)
        lane = self.store.get_control_lane_state("worker")
        self.assertEqual("running", lane.state.value if lane else None)
        status = self.store.get_control_system_state()
        self.assertEqual("healthy", status.global_state.value)
        actions = self.store.list_control_recovery_actions(target_type="lane", target_id="worker")
        self.assertEqual("resume", actions[0].action_type)

    def test_sa_watch_can_request_stack_restart(self) -> None:
        restarted: list[dict[str, object]] = []
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        json.dumps(
                            {
                                "action": "restart_stack",
                                "reason": "The workflow appears wedged and should be restarted onto the latest code.",
                                "confidence": 0.82,
                                "target_lane": "harness",
                                "escalate": False,
                            }
                        )
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
            restart_stack=lambda payload: restarted.append(payload) or self.control_plane.status(),
        )

        result = service.observe({"type": "sleeping"})

        self.assertIsNotNone(result)
        self.assertEqual(1, len(restarted))
        self.assertEqual("sa_watch_requested_restart", restarted[0]["reason"])
        actions = self.store.list_control_recovery_actions(target_type="system", target_id="system")
        self.assertEqual("restart", actions[0].action_type)

    def test_sa_watch_loop_idles_when_control_plane_or_harness_inactive(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "sa-watch-loop.db",
            workspace_root=root / "sa-watch-workspace",
            log_path=root / "sa-watch.log",
            config_file=root / "sa-watch-config.json",
        )
        inactive_store = SQLiteHarnessStore(config.db_path)
        inactive_store.initialize()
        inactive_control_plane = ControlPlane(inactive_store)
        inactive_control_plane.turn_on()
        inactive_control_watch = ControlWatchService(
            inactive_store,
            inactive_control_plane,
            FailureClassifier(),
            BreadcrumbWriter(inactive_store, config.workspace_root),
            supervisor_control_dir=root / "supervisors",
        )
        ctx = SimpleNamespace(
            config=config,
            store=inactive_store,
            control_plane=inactive_control_plane,
            control_watch=inactive_control_watch,
            sa_watch=SimpleNamespace(run_once=lambda: {"decision": {"action": "none", "reason": "noop"}}),
        )
        args = Namespace(command="sa-watch-loop", interval_seconds=0.0, max_iterations=1)

        with patch("accruvia_harness.commands.control.emit") as emit_mock:
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        emitted = emit_mock.call_args.args[0]
        self.assertEqual("idle", emitted["mode"])
        self.assertFalse(sa_watch_runtime_state_path(config).exists())

    def test_sa_watch_loop_runs_when_control_plane_and_harness_are_running(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "sa-watch-active.db",
            workspace_root=root / "sa-watch-active-workspace",
            log_path=root / "sa-watch-active.log",
            config_file=root / "sa-watch-active-config.json",
        )
        active_store = SQLiteHarnessStore(config.db_path)
        active_store.initialize()
        active_control_plane = ControlPlane(active_store)
        active_control_plane.turn_on()
        supervisor_dir = root / "supervisors"
        supervisor_dir.mkdir(parents=True, exist_ok=True)
        (supervisor_dir / "sa_watch_test.json").write_text(
            json.dumps({"pid": os.getpid(), "project_id": "project_123", "worker_id": "supervisor"}),
            encoding="utf-8",
        )
        active_control_watch = ControlWatchService(
            active_store,
            active_control_plane,
            FailureClassifier(),
            BreadcrumbWriter(active_store, config.workspace_root),
            supervisor_control_dir=supervisor_dir,
        )
        sa_watch_mock = SimpleNamespace(run_once=lambda: {"decision": {"action": "resume_worker", "reason": "continue"}})
        ctx = SimpleNamespace(
            config=config,
            store=active_store,
            control_plane=active_control_plane,
            control_watch=active_control_watch,
            sa_watch=sa_watch_mock,
        )
        args = Namespace(command="sa-watch-loop", interval_seconds=0.0, max_iterations=1)

        with patch("accruvia_harness.commands.control.emit") as emit_mock:
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        emitted = emit_mock.call_args.args[0]
        self.assertEqual("active", emitted["mode"])
        self.assertEqual("resume_worker", emitted["result"]["decision"]["action"])
        self.assertFalse(sa_watch_runtime_state_path(config).exists())

    def test_sa_watch_loop_prints_human_readable_summary_and_unusable_model_response(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "sa-watch-readable.db",
            workspace_root=root / "sa-watch-readable-workspace",
            log_path=root / "sa-watch-readable.log",
            config_file=root / "sa-watch-readable-config.json",
        )
        readable_store = SQLiteHarnessStore(config.db_path)
        readable_store.initialize()
        readable_control_plane = ControlPlane(readable_store)
        readable_control_plane.turn_on()
        supervisor_dir = root / "supervisors"
        supervisor_dir.mkdir(parents=True, exist_ok=True)
        (supervisor_dir / "readable_supervisor.json").write_text(
            json.dumps({"pid": os.getpid(), "worker_id": "supervisor"}),
            encoding="utf-8",
        )
        readable_control_watch = ControlWatchService(
            readable_store,
            readable_control_plane,
            FailureClassifier(),
            BreadcrumbWriter(readable_store, config.workspace_root),
            supervisor_control_dir=supervisor_dir,
        )
        ctx = SimpleNamespace(
            config=config,
            store=readable_store,
            control_plane=readable_control_plane,
            control_watch=readable_control_watch,
            sa_watch=SimpleNamespace(
                run_once=lambda: {
                    "decision": {"action": "model_response_unusable", "reason": "sa-watch returned no reason"},
                    "packet": {"continuity_signals": []},
                    "effects": [{"kind": "model_response_unusable", "reason": "sa-watch returned no reason"}],
                }
            ),
        )
        args = Namespace(command="sa-watch-loop", interval_seconds=0.0, max_iterations=1)

        with (
            patch("accruvia_harness.commands.control.emit"),
            patch("accruvia_harness.commands.control.print") as print_mock,
        ):
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        printed = "\n".join(call.args[0] for call in print_mock.call_args_list if call.args)
        self.assertIn("workflow state: IDLE (no pending or active tasks)", printed)
        self.assertIn("decision: could not make a trustworthy decision; signals: none; reason: unavailable", printed)
        self.assertIn("summary: totals [tasks completed 0, objectives completed 0, pending 0, active 0, stalled objectives 0]", printed)
        self.assertIn("deltas [tasks completed n/a, objectives completed n/a, pending n/a, active n/a, stalled objectives n/a]", printed)
        self.assertIn("changed code/workflow: no", printed)
        self.assertIn("could not make a trustworthy decision; no additional action taken", printed)

    def test_sa_watch_workflow_state_line_marks_unplugged(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "sa-watch-unplugged.db",
            workspace_root=root / "sa-watch-unplugged-workspace",
            log_path=root / "sa-watch-unplugged.log",
            config_file=root / "sa-watch-unplugged-config.json",
        )
        unplugged_store = SQLiteHarnessStore(config.db_path)
        unplugged_store.initialize()
        unplugged_control_plane = ControlPlane(unplugged_store)
        unplugged_control_plane.turn_on()
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        unplugged_store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Stalled objective",
            summary="stalled",
            status=ObjectiveStatus.PLANNING,
        )
        unplugged_store.create_objective(objective)
        unplugged_store.create_control_event(
            ControlEvent(
                id=new_id("control_event"),
                event_type="objective_stalled",
                entity_type="objective",
                entity_id=objective.id,
                producer="test",
                payload={"objective_id": objective.id},
                idempotency_key=new_id("event_key"),
            )
        )
        TaskService(unplugged_store).create_task_with_policy(
            project_id=project.id,
            objective_id=objective.id,
            title="Unblock stalled objective workflow",
            objective="Repair it.",
            priority=150,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="sa_watch",
            external_ref_id=f"objective:{objective.id}:objective_stalled",
            strategy="sa_structural_fix",
        )
        supervisor_dir = root / "supervisors"
        supervisor_dir.mkdir(parents=True, exist_ok=True)
        (supervisor_dir / "unplugged_supervisor.json").write_text(
            json.dumps({"pid": os.getpid(), "worker_id": "supervisor"}),
            encoding="utf-8",
        )
        unplugged_control_watch = ControlWatchService(
            unplugged_store,
            unplugged_control_plane,
            FailureClassifier(),
            BreadcrumbWriter(unplugged_store, config.workspace_root),
            supervisor_control_dir=supervisor_dir,
        )
        ctx = SimpleNamespace(
            config=config,
            store=unplugged_store,
            control_plane=unplugged_control_plane,
            control_watch=unplugged_control_watch,
            sa_watch=SimpleNamespace(run_once=lambda: {"decision": {"action": "none", "reason": "noop"}, "packet": {"continuity_signals": []}, "effects": []}),
        )
        args = Namespace(command="sa-watch-loop", interval_seconds=300.0, max_iterations=1)

        with (
            patch("accruvia_harness.commands.control.emit"),
            patch("accruvia_harness.commands.control.print") as print_mock,
        ):
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        printed = "\n".join(call.args[0] for call in print_mock.call_args_list if call.args)
        self.assertIn("workflow state: UNPLUGGED (1 stalled objective, 1 pending task, 0 active)", printed)

    def test_sa_watch_loop_uses_short_startup_grace_before_first_check(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "sa-watch-grace.db",
            workspace_root=root / "sa-watch-grace-workspace",
            log_path=root / "sa-watch-grace.log",
            config_file=root / "sa-watch-grace-config.json",
        )
        grace_store = SQLiteHarnessStore(config.db_path)
        grace_store.initialize()
        grace_control_plane = ControlPlane(grace_store)
        grace_control_plane.turn_on()
        supervisor_dir = root / "supervisors"
        supervisor_dir.mkdir(parents=True, exist_ok=True)
        (supervisor_dir / "grace_supervisor.json").write_text(
            json.dumps({"pid": os.getpid(), "worker_id": "supervisor"}),
            encoding="utf-8",
        )
        grace_control_watch = ControlWatchService(
            grace_store,
            grace_control_plane,
            FailureClassifier(),
            BreadcrumbWriter(grace_store, config.workspace_root),
            supervisor_control_dir=supervisor_dir,
        )
        called = {"count": 0}
        sa_watch_mock = SimpleNamespace(run_once=lambda: called.__setitem__("count", called["count"] + 1) or {"decision": {"action": "resume_worker", "reason": "continue"}})
        ctx = SimpleNamespace(
            config=config,
            store=grace_store,
            control_plane=grace_control_plane,
            control_watch=grace_control_watch,
            sa_watch=sa_watch_mock,
        )
        args = Namespace(command="sa-watch-loop", interval_seconds=300.0, max_iterations=6)

        with patch("accruvia_harness.commands.control.emit") as emit_mock:
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        self.assertGreaterEqual(called["count"], 1)
        emitted = emit_mock.call_args.args[0]
        self.assertEqual("active", emitted["mode"])

    def test_sa_watch_start_records_desired_state_and_spawns_process(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "sa-watch-start.db",
            workspace_root=root / "sa-watch-start-workspace",
            log_path=root / "sa-watch-start.log",
            config_file=root / "sa-watch-start-config.json",
        )
        ctx = SimpleNamespace(config=config, control_plane=None, control_watch=None, store=None, sa_watch=None)
        args = Namespace(command="sa-watch-start", interval_seconds=321.0)

        with (
            patch("accruvia_harness.commands.control.emit") as emit_mock,
            patch("accruvia_harness.commands.common.subprocess.check_output", return_value=""),
            patch("accruvia_harness.commands.common.subprocess.Popen") as popen,
        ):
            popen.return_value.pid = 4242
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        emitted = emit_mock.call_args.args[0]
        self.assertEqual(4242, emitted["pid"])
        desired = json.loads(desired_sa_watch_state_path(config).read_text(encoding="utf-8"))
        self.assertEqual(321.0, desired["interval_seconds"])

    def test_sa_watch_status_reports_desired_runtime_and_liveness(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "sa-watch-status.db",
            workspace_root=root / "sa-watch-status-workspace",
            log_path=root / "sa-watch-status.log",
            config_file=root / "sa-watch-status-config.json",
        )
        status_store = SQLiteHarnessStore(config.db_path)
        status_store.initialize()
        status_control_plane = ControlPlane(status_store)
        status_control_plane.turn_on()
        supervisor_dir = root / "supervisors"
        supervisor_dir.mkdir(parents=True, exist_ok=True)
        (supervisor_dir / "status_supervisor.json").write_text(
            json.dumps({"pid": os.getpid(), "worker_id": "supervisor"}),
            encoding="utf-8",
        )
        status_control_watch = ControlWatchService(
            status_store,
            status_control_plane,
            FailureClassifier(),
            BreadcrumbWriter(status_store, config.workspace_root),
            supervisor_control_dir=supervisor_dir,
        )
        desired_sa_watch_state_path(config).write_text(json.dumps({"interval_seconds": 600.0}), encoding="utf-8")
        sa_watch_runtime_state_path(config).write_text(
            json.dumps({"pid": os.getpid(), "interval_seconds": 600.0, "mode": "active"}),
            encoding="utf-8",
        )
        ctx = SimpleNamespace(
            config=config,
            store=status_store,
            control_plane=status_control_plane,
            control_watch=status_control_watch,
        )
        args = Namespace(command="sa-watch-status")

        with patch("accruvia_harness.commands.control.emit") as emit_mock:
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        emitted = emit_mock.call_args.args[0]
        self.assertTrue(emitted["running"])
        self.assertTrue(emitted["active"])
        self.assertEqual(600.0, emitted["desired"]["interval_seconds"])

    def test_sa_watch_stop_clears_desired_and_runtime_state(self) -> None:
        root = Path(self.temp_dir.name)
        config = HarnessConfig.from_env(
            db_path=root / "sa-watch-stop.db",
            workspace_root=root / "sa-watch-stop-workspace",
            log_path=root / "sa-watch-stop.log",
            config_file=root / "sa-watch-stop-config.json",
        )
        desired_sa_watch_state_path(config).write_text(json.dumps({"interval_seconds": 600.0}), encoding="utf-8")
        sa_watch_runtime_state_path(config).write_text(json.dumps({"pid": os.getpid(), "mode": "active"}), encoding="utf-8")
        ctx = SimpleNamespace(config=config, control_plane=None, control_watch=None, store=None, sa_watch=None)
        args = Namespace(command="sa-watch-stop")

        with (
            patch("accruvia_harness.commands.control.emit") as emit_mock,
            patch("accruvia_harness.commands.common._terminate_pid") as terminate_pid,
        ):
            handled = handle_control_command(args, ctx)

        self.assertTrue(handled)
        terminate_pid.assert_called_once_with(os.getpid())
        self.assertFalse(desired_sa_watch_state_path(config).exists())
        self.assertFalse(sa_watch_runtime_state_path(config).exists())
        emitted = emit_mock.call_args.args[0]
        self.assertTrue(emitted["stopped"])

    def test_sa_watch_records_new_direct_repair_evidence_after_prior_structural_fix_failed(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Broken objective",
            summary="broken",
            status=ObjectiveStatus.PAUSED,
        )
        self.store.create_objective(objective)
        failing_task = TaskService(self.store).create_task_with_policy(
            project_id=project.id,
            objective_id=objective.id,
            title="Retrying task",
            objective="Keep failing the same way.",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="default",
            max_attempts=4,
        )
        prior_fix = TaskService(self.store).create_task_with_policy(
            project_id=project.id,
            objective_id=objective.id,
            title="Old structural fix",
            objective="Old failed fix.",
            priority=150,
            parent_task_id=failing_task.id,
            source_run_id=None,
            external_ref_type="sa_watch",
            external_ref_id=f"{failing_task.id}:timeout",
            strategy="sa_structural_fix",
            max_attempts=2,
        )
        self.store.update_task_status(prior_fix.id, TaskStatus.FAILED)
        self.store.upsert_control_worker_run(
            ControlWorkerRun(id="run_1", task_id=failing_task.id, status="failed", classification="timeout")
        )
        self.store.upsert_control_worker_run(
            ControlWorkerRun(id="run_2", task_id=failing_task.id, status="failed", classification="timeout")
        )
        self.control_plane.pause_lane("worker", reason="timeout")
        self.control_plane.mark_degraded("timeout")
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        json.dumps(
                            {
                                "action": "repair_harness",
                                "reason": "The prior structural fix failed; create a narrower recurrence-prevention task.",
                                "confidence": 0.9,
                                "target_lane": "worker",
                                "target_task_id": failing_task.id,
                                "task_title": "Retry structural fix",
                                "task_objective": "Prevent recurrence with a narrower fix and proof.",
                                "escalate": False,
                            }
                        )
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
            repair_runner=lambda task, run, repo_root: SAWatchRepairResult(
                status="failed",
                run_id=run.id,
                run_dir=self.workspace_root / "control" / "sa_watch_repairs" / run.id,
                summary="repair still failing",
                changed_files=["src/accruvia_harness/sa_watch.py"],
                validation={"compile_check": {"ok": False}, "test_check": {"ok": False}},
                diagnostics={"failure_message": "repair still failing"},
            ),
        )

        service.observe({"type": "sleeping"})

        repair_records = self.store.list_context_records(objective_id=objective.id, record_type="sa_watch_repair")
        self.assertEqual(1, len(repair_records))
        self.assertIn("repair still failing", json.dumps(repair_records[0].metadata))
        self.assertEqual("six_whys", repair_records[0].metadata["blameless_review"]["method"])
        self.assertEqual(6, len(repair_records[0].metadata["blameless_review"]["why_chain"]))

    def test_sa_watch_repairs_harness_directly_for_stale_atomic_generation(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Stalled decomposition objective",
            summary="A stale atomic generation should trigger structural intervention.",
            status=ObjectiveStatus.PAUSED,
        )
        self.store.create_objective(objective)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_started",
                project_id=project.id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Started generating atomic units from Mermaid v1.",
                metadata={"generation_id": "atomic_generation_stale", "diagram_version": 1},
                created_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_progress",
                project_id=project.id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Atomic decomposition round 8.",
                metadata={
                    "generation_id": "atomic_generation_stale",
                    "diagram_version": 1,
                    "phase": "round 8: critique + coverage + refine",
                },
                created_at=datetime.now(UTC) - timedelta(minutes=9),
            )
        )
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        json.dumps(
                            {
                                "action": "repair_harness",
                                "reason": "Atomic decomposition stopped making forward progress and needs a structural workflow fix.",
                                "confidence": 0.95,
                                "target_lane": "worker",
                                "target_task_id": None,
                                "task_title": "Fix stale atomic decomposition recovery",
                                "task_objective": "Prevent stale atomic generation loops and prove the objective advances afterward.",
                                "escalate": False,
                            }
                        )
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
            repair_runner=lambda task, run, repo_root: SAWatchRepairResult(
                status="validated",
                run_id=run.id,
                run_dir=self.workspace_root / "control" / "sa_watch_repairs" / run.id,
                summary="repaired stale atomic recovery",
                changed_files=["src/accruvia_harness/sa_watch.py"],
                validation={"compile_check": {"ok": True}, "test_check": {"ok": True}},
                diagnostics={"worker_outcome": "success"},
            ),
            post_repair_callback=lambda _task: (
                self.store.update_objective_status(objective.id, ObjectiveStatus.PLANNING),
                TaskService(self.store).create_task_with_policy(
                    project_id=project.id,
                    objective_id=objective.id,
                    title="Resume after direct repair",
                    objective="Continue atomic generation after repair.",
                    priority=100,
                    parent_task_id=None,
                    source_run_id=None,
                    external_ref_type=None,
                    external_ref_id=None,
                    strategy="atomic_from_mermaid",
                ),
            ),
        )

        result = service.observe({"type": "sleeping"})

        self.assertIsNotNone(result)
        self.assertEqual("repair_harness", result["decision"]["action"])
        self.assertIsNone(self.store.get_task_by_external_ref("sa_watch", f"objective:{objective.id}:stale_atomic_generation"))
        repair_records = self.store.list_context_records(objective_id=objective.id, record_type="sa_watch_repair")
        self.assertEqual(1, len(repair_records))
        self.assertTrue(repair_records[0].metadata["blameless_review"]["blameless"])
        self.assertIn("repair_artifact_inventory", repair_records[0].metadata)
        repair_tasks = [task for task in self.store.list_tasks(project.id) if task.strategy == "sa_watch_direct_repair"]
        self.assertEqual(1, len(repair_tasks))
        self.assertEqual(TaskStatus.COMPLETED, repair_tasks[0].status)
        repair_runs = self.store.list_runs(repair_tasks[0].id)
        self.assertEqual(1, len(repair_runs))
        self.assertEqual(RunStatus.COMPLETED, repair_runs[0].status)
        objective_after = self.store.get_objective(objective.id)
        self.assertEqual(ObjectiveStatus.PLANNING, objective_after.status if objective_after else None)

    def test_sa_watch_repairs_and_restarts_after_direct_harness_fix(self) -> None:
        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Stalled decomposition objective",
            summary="A stale atomic generation should trigger structural intervention.",
            status=ObjectiveStatus.PAUSED,
        )
        self.store.create_objective(objective)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_started",
                project_id=project.id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Started generating atomic units from Mermaid v1.",
                metadata={"generation_id": "atomic_generation_stale", "diagram_version": 1},
                created_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_progress",
                project_id=project.id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Atomic decomposition round 8.",
                metadata={
                    "generation_id": "atomic_generation_stale",
                    "diagram_version": 1,
                    "phase": "round 8: critique + coverage + refine",
                },
                created_at=datetime.now(UTC) - timedelta(minutes=9),
            )
        )
        self.control_plane.pause_lane("worker", reason="stale_atomic_generation")
        self.control_plane.mark_degraded("stale_atomic_generation")
        restarted: list[dict[str, object]] = []

        def _post_repair(_task) -> None:
            self.store.update_objective_status(objective.id, ObjectiveStatus.EXECUTING)
            resumed = TaskService(self.store).create_task_with_policy(
                project_id=project.id,
                objective_id=objective.id,
                title="Resume after hot patch",
                objective="Continue objective execution on the fixed path.",
                priority=100,
                parent_task_id=None,
                source_run_id=None,
                external_ref_type=None,
                external_ref_id=None,
                strategy="atomic_from_mermaid",
            )
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="atomic_generation_started",
                    project_id=project.id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content="Restarted generation after hot patch.",
                    metadata={"generation_id": "atomic_generation_resumed", "diagram_version": 2},
                )
            )
            self.assertEqual(TaskStatus.PENDING, resumed.status)

        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        json.dumps(
                            {
                                "action": "repair_harness",
                                "reason": "Stale atomic generation requires an architectural workflow fix.",
                                "confidence": 0.95,
                                "target_lane": "worker",
                                "target_task_id": None,
                                "task_title": "Fix stale atomic decomposition recovery",
                                "task_objective": "Patch the decomposition workflow so stale generations recover structurally and prove progress resumes.",
                                "escalate": False,
                            }
                        )
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
            repair_runner=lambda task, run, repo_root: SAWatchRepairResult(
                status="validated",
                run_id=run.id,
                run_dir=self.workspace_root / "control" / "sa_watch_repairs" / run.id,
                summary="direct repair validated",
                changed_files=["src/accruvia_harness/sa_watch.py"],
                validation={"compile_check": {"ok": True}, "test_check": {"ok": True}},
                diagnostics={"worker_outcome": "success"},
            ),
            post_repair_callback=_post_repair,
            restart_stack=lambda payload: restarted.append(payload) or self.control_plane.status(),
        )

        result = service.observe({"type": "sleeping"})

        self.assertIsNotNone(result)
        self.assertEqual("repair_harness", result["decision"]["action"])
        self.assertEqual(1, len(restarted))
        lane = self.store.get_control_lane_state("worker")
        self.assertEqual("running", lane.state.value if lane else None)
        status = self.store.get_control_system_state()
        self.assertEqual("healthy", status.global_state.value)

    def test_sa_watch_prefers_live_stale_atomic_generation_over_old_objective_stall_event(self) -> None:
        old_project = Project(id=new_id("project"), name="old-project", description="old")
        self.store.create_project(old_project)
        old_objective = Objective(
            id=new_id("objective"),
            project_id=old_project.id,
            title="Old stalled objective",
            summary="old",
            status=ObjectiveStatus.PAUSED,
        )
        self.store.create_objective(old_objective)
        self.store.create_control_event(
            ControlEvent(
                id=new_id("control_event"),
                event_type="objective_stalled",
                entity_type="objective",
                entity_id=old_objective.id,
                producer="test",
                payload={"objective_id": old_objective.id},
                idempotency_key=new_id("event_key"),
                created_at=datetime.now(UTC) - timedelta(hours=2),
            )
        )

        project = Project(id=new_id("project"), name="watch-project", description="watch")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Live stale decomposition objective",
            summary="live",
            status=ObjectiveStatus.PAUSED,
        )
        self.store.create_objective(objective)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_started",
                project_id=project.id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Started generating atomic units from Mermaid v1.",
                metadata={"generation_id": "atomic_generation_live", "diagram_version": 1},
                created_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_progress",
                project_id=project.id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Atomic decomposition round 8.",
                metadata={
                    "generation_id": "atomic_generation_live",
                    "diagram_version": 1,
                    "phase": "round 8: critique + coverage + refine",
                },
                created_at=datetime.now(UTC) - timedelta(minutes=9),
            )
        )
        service = SAWatchService(
            self.store,
            self.control_plane,
            LLMRouter(
                "codex",
                {
                    "codex": FakeExecutor(
                        json.dumps(
                            {
                                "action": "repair_harness",
                                "reason": "Live stale decomposition needs a structural fix.",
                                "confidence": 0.93,
                                "target_lane": "worker",
                                "task_title": "Fix live stale atomic decomposition",
                                "task_objective": "Repair the current stale atomic workflow and prove the objective advances.",
                                "escalate": False,
                            }
                        )
                    )
                },
            ),
            self.workspace_root,
            interval_seconds=0,
        )

        result = service.observe({"type": "sleeping"})

        self.assertIsNotNone(result)
        self.assertEqual("stale_atomic_generation", result["packet"]["structural_signal"]["kind"])


class QueueServiceWorkerLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()
        self.control_plane = ControlPlane(self.store)
        self.control_plane.turn_on()
        self.tasks = TaskService(self.store)
        self.runner = FakeRunner()
        self.queue = QueueService(self.store, self.runner)
        self.project = self.tasks.create_project("queue-project", "queue")

    def test_paused_worker_lane_blocks_all_tasks(self) -> None:
        normal_task = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=None,
            title="Normal work",
            objective="Do normal work.",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="default",
        )
        structural_task = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=None,
            title="Structural fix",
            objective="Repair the recurring defect.",
            priority=200,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="sa_watch",
            external_ref_id="structural:1",
            strategy="sa_structural_fix",
        )
        self.control_plane.pause_lane("worker", reason="timeout")

        result = self.queue.process_next_task(worker_id="tester")

        self.assertIsNone(result)
        self.assertEqual([], self.runner.ran_task_ids)
        refreshed_normal = self.store.get_task(normal_task.id)
        self.assertEqual(TaskStatus.PENDING, refreshed_normal.status if refreshed_normal else None)

    def test_structural_fix_task_no_longer_bypasses_objective_gate(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Blocked objective",
            summary="blocked by missing readiness artifacts",
            status=ObjectiveStatus.PLANNING,
        )
        self.store.create_objective(objective)
        structural_task = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=objective.id,
            title="Structural fix",
            objective="Repair the blocked workflow.",
            priority=200,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="sa_watch",
            external_ref_id=f"objective:{objective.id}:workflow_gap",
            strategy="sa_structural_fix",
        )

        result = self.queue.process_next_task(worker_id="tester")

        self.assertIsNone(result)
        self.assertEqual([], self.runner.ran_task_ids)

    def test_objective_review_remediation_task_can_run_even_when_objective_is_blocked(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Blocked objective",
            summary="blocked",
            status=ObjectiveStatus.PLANNING,
        )
        self.store.create_objective(objective)
        remediation_task = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=objective.id,
            title="Objective review remediation",
            objective="Fix the reviewer finding and produce the required evidence artifact.",
            priority=150,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="objective_review",
            external_ref_id=f"{objective.id}:review_1:unit_test_coverage",
            strategy="objective_review_remediation",
        )

        result = self.queue.process_next_task(worker_id="tester")

        self.assertIsNotNone(result)
        self.assertEqual(remediation_task.id, result["task"].id if result is not None else None)
        self.assertEqual([remediation_task.id], self.runner.ran_task_ids)

    def test_objective_budget_exhaustion_skips_blocked_objective_and_runs_next_objective(self) -> None:
        blocked_objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Blocked objective",
            summary="over budget",
            status=ObjectiveStatus.EXECUTING,
        )
        runnable_objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Runnable objective",
            summary="ready",
            status=ObjectiveStatus.EXECUTING,
        )
        self.store.create_objective(blocked_objective)
        self.store.create_objective(runnable_objective)
        blocked_task = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=blocked_objective.id,
            title="Blocked work",
            objective="Should be skipped due to objective budget exhaustion.",
            priority=200,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="objective_review",
            external_ref_id=f"{blocked_objective.id}:review_1:budget",
            strategy="objective_review_remediation",
        )
        runnable_task = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=runnable_objective.id,
            title="Runnable work",
            objective="Should run after the blocked objective is skipped.",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="objective_review",
            external_ref_id=f"{runnable_objective.id}:review_1:budget",
            strategy="objective_review_remediation",
        )
        for _ in range(4):
            self.control_plane.record_budget_usage(budget_scope="objective", budget_key=blocked_objective.id)

        result = self.queue.process_next_task(worker_id="tester")

        self.assertIsNotNone(result)
        self.assertEqual(runnable_task.id, result["task"].id if result is not None else None)
        self.assertEqual([runnable_task.id], self.runner.ran_task_ids)
        self.assertEqual(TaskStatus.PENDING, self.store.get_task(blocked_task.id).status)

    def test_objective_no_progress_skip_allows_other_objectives_to_run(self) -> None:
        blocked_objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="No progress objective",
            summary="stalled",
            status=ObjectiveStatus.EXECUTING,
        )
        runnable_objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Fresh objective",
            summary="ready",
            status=ObjectiveStatus.EXECUTING,
        )
        self.store.create_objective(blocked_objective)
        self.store.create_objective(runnable_objective)
        blocked_task = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=blocked_objective.id,
            title="Blocked work",
            objective="Should be skipped due to no_progress.",
            priority=200,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="objective_review",
            external_ref_id=f"{blocked_objective.id}:review_1:no_progress",
            strategy="objective_review_remediation",
        )
        runnable_task = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=runnable_objective.id,
            title="Runnable work",
            objective="Should run after skipping no_progress objective.",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="objective_review",
            external_ref_id=f"{runnable_objective.id}:review_1:no_progress",
            strategy="objective_review_remediation",
        )
        self.control_plane.record_human_escalation(
            "no_progress",
            payload={
                "objective_id": blocked_objective.id,
                "reason": "Three completed coding runs did not advance the objective to a mergeable state.",
            },
        )

        result = self.queue.process_next_task(worker_id="tester")

        self.assertIsNotNone(result)
        self.assertEqual(runnable_task.id, result["task"].id if result is not None else None)
        self.assertEqual([runnable_task.id], self.runner.ran_task_ids)
        self.assertEqual(TaskStatus.PENDING, self.store.get_task(blocked_task.id).status)


class WorkflowServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()
        self.tasks = TaskService(self.store)
        self.workflow = WorkflowService(self.store)
        self.project = self.tasks.create_project("workflow-project", "workflow")

    def test_queue_state_marks_structural_fix_blocked_when_objective_is_blocked(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Blocked objective",
            summary="blocked",
            status=ObjectiveStatus.PLANNING,
        )
        self.store.create_objective(objective)
        task = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=objective.id,
            title="Structural fix",
            objective="Repair the blocked workflow.",
            priority=150,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="sa_watch",
            external_ref_id=f"objective:{objective.id}:workflow_gap",
            strategy="sa_structural_fix",
        )

        queue_state = self.workflow.queue_state_for_task(task)

        self.assertEqual("blocked_by_gate", queue_state["state"])

    def test_queue_state_marks_objective_review_remediation_runnable_even_when_objective_is_blocked(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Blocked objective",
            summary="blocked",
            status=ObjectiveStatus.PLANNING,
        )
        self.store.create_objective(objective)
        task = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=objective.id,
            title="Review remediation",
            objective="Address review findings and provide evidence.",
            priority=150,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="objective_review",
            external_ref_id=f"{objective.id}:review_1:unit_test_coverage",
            strategy="objective_review_remediation",
        )

        queue_state = self.workflow.queue_state_for_task(task)

        self.assertEqual("runnable", queue_state["state"])


class SuperviseProgressTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()
        self.tasks = TaskService(self.store)
        self.workflow = WorkflowService(self.store)
        self.project = self.tasks.create_project("workflow-project", "workflow")

    def test_worker_status_operator_text_for_active_structural_fix(self) -> None:
        text = _worker_status_operator_text(
            {
                "strategy": "sa_structural_fix",
                "latest_artifact": "plan.txt",
                "latest_artifact_kind": "plan",
                "latest_artifact_path": "/tmp/run/plan.txt",
                "latest_artifact_age_seconds": 359,
                "stale": False,
            }
        )
        self.assertEqual(
            "recovery run active; no new durable artifacts for 05:59 (latest plan plan.txt @ /tmp/run/plan.txt)",
            text,
        )

    def test_worker_status_operator_text_for_stale_structural_fix(self) -> None:
        text = _worker_status_operator_text(
            {
                "strategy": "sa_structural_fix",
                "latest_artifact": "plan.txt",
                "latest_artifact_kind": "plan",
                "latest_artifact_path": "/tmp/run/plan.txt",
                "latest_artifact_age_seconds": 1200,
                "stale": True,
            }
        )
        self.assertEqual(
            "recovery run likely stuck; no new durable artifacts for 20:00 (latest plan plan.txt @ /tmp/run/plan.txt)",
            text,
        )

    def test_reconcile_restarts_atomic_generation_when_only_terminal_failed_work_remains(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project.id,
            title="Restart decomposition",
            summary="restart atomic generation",
            status=ObjectiveStatus.PAUSED,
        )
        self.store.create_objective(objective)
        completed = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=objective.id,
            title="Completed remediation",
            objective="done",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
        )
        failed = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=objective.id,
            title="Failed remediation",
            objective="failed",
            priority=110,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
        )
        self.store.update_task_status(completed.id, TaskStatus.COMPLETED)
        self.store.update_task_status(failed.id, TaskStatus.FAILED)
        started: list[str] = []

        with patch(
            "accruvia_harness.services.workflow_service.objective_execution_gate",
            return_value=ObjectiveExecutionGate(objective_id=objective.id, ready=True, gate_checks=[]),
        ):
            result = self.workflow.reconcile_objective(
                objective.id,
                start_atomic=lambda oid: started.append(oid),
                atomic_running=False,
                review_running=False,
                review_start_allowed=False,
            )

        self.assertEqual([objective.id], started)
        self.assertIn("restart_atomic_generation", result["actions"])


class ControlPlaneStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(root / "harness.db")
        self.store.initialize()
        self.control_plane = ControlPlane(self.store)
        self.control_plane.turn_on()
        self.tasks = TaskService(self.store)
        self.project = self.tasks.create_project("status-project", "status")

    def test_status_output_is_deterministic_without_model(self) -> None:
        task = self.tasks.create_task_with_policy(
            project_id=self.project.id,
            objective_id=None,
            title="Status task",
            objective="Surface active lease deterministically.",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
        )
        leased = self.store.acquire_task_lease("tester", 300, self.project.id)
        self.assertIsNotNone(leased)
        self.control_plane.pause_lane("worker", reason="timeout")

        status = self.control_plane.status()

        self.assertEqual("degraded", status["global_state"])
        self.assertEqual("paused", status["lanes"]["worker"])
        self.assertEqual(task.id, status["active_task_id"])

    def test_budget_exhaustion_is_scope_specific(self) -> None:
        for _ in range(4):
            self.control_plane.record_budget_usage(budget_scope="objective", budget_key="objective_123")

        self.assertTrue(
            self.control_plane.expensive_coding_budget_exhausted(
                budget_scope="objective",
                budget_key="objective_123",
            )
        )
        self.assertFalse(self.control_plane.expensive_coding_budget_exhausted())
