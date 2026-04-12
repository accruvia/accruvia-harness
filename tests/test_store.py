from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from accruvia_harness.domain import (
    ControlBreadcrumb,
    ControlEvent,
    ControlLaneStateValue,
    ControlRecoveryAction,
    ControlWorkerRun,
    GlobalSystemState,
    ContextRecord,
    Event,
    Evaluation,
    EvaluationVerdict,
    IntentModel,
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
from accruvia_harness.control_plane import ControlPlane
from accruvia_harness.migrations import Migration, apply_migrations
from accruvia_harness.services.workflow_service import WorkflowService
from accruvia_harness.store import SQLiteHarnessStore


class SQLiteHarnessStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "harness.db"
        self.store = SQLiteHarnessStore(self.db_path)
        self.store.initialize()

    def test_task_round_trip_preserves_policy_fields(self) -> None:
        project = Project(
            id=new_id("project"),
            name="accruvia",
            description="Harness work",
            adapter_name="generic",
        )
        self.store.create_project(project)
        linked_objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Linked objective",
            summary="Persist task linkage",
            status=ObjectiveStatus.OPEN,
        )
        self.store.create_objective(linked_objective)

        task = Task(
            id=new_id("task"),
            project_id=project.id,
            objective_id=linked_objective.id,
            title="Runner task",
            objective="Exercise policy persistence",
            priority=250,
            parent_task_id="task_parent",
            source_run_id="run_source",
            external_ref_type="gitlab_issue",
            external_ref_id="456",
            external_ref_metadata={"labels": ["bug"], "milestone": "MVP", "assignees": ["sanaani"]},
            validation_profile="python",
            validation_mode="lightweight_repair",
            scope={"allowed_paths": ["src/demo.py"], "forbidden_paths": ["README.md"]},
            strategy="baseline",
            max_attempts=5,
            required_artifacts=["plan", "report", "diff"],
        )
        self.store.create_task(task)

        loaded = self.store.get_task(task.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(250, loaded.priority)
        self.assertEqual(linked_objective.id, loaded.objective_id)
        self.assertEqual("task_parent", loaded.parent_task_id)
        self.assertEqual("run_source", loaded.source_run_id)
        self.assertEqual("gitlab_issue", loaded.external_ref_type)
        self.assertEqual("456", loaded.external_ref_id)
        self.assertEqual(["bug"], loaded.external_ref_metadata["labels"])
        self.assertEqual("python", loaded.validation_profile)
        self.assertEqual("lightweight_repair", loaded.validation_mode)
        self.assertEqual(["src/demo.py"], loaded.scope["allowed_paths"])
        self.assertEqual(["README.md"], loaded.scope["forbidden_paths"])
        self.assertEqual("baseline", loaded.strategy)
        self.assertEqual(5, loaded.max_attempts)
        self.assertEqual(["plan", "report", "diff"], loaded.required_artifacts)

    def test_update_objective_phase_resolves_when_only_failed_tasks_are_waived_obsolete(self) -> None:
        project = Project(
            id=new_id("project"),
            name="phase-project",
            description="Objective phase persistence",
            adapter_name="generic",
        )
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Objective phase",
            summary="Waived obsolete failures should not keep the objective planning.",
            status=ObjectiveStatus.PLANNING,
        )
        self.store.create_objective(objective)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            objective_id=objective.id,
            title="Obsolete failed task",
            objective="Superseded path",
            status=TaskStatus.FAILED,
            external_ref_metadata={
                "failed_task_disposition": {
                    "kind": "waive_obsolete",
                    "rationale": "Superseded by manual implementation.",
                }
            },
        )
        self.store.create_task(task)

        phase = self.store.update_objective_phase(objective.id)
        objective_after = self.store.get_objective(objective.id)

        self.assertEqual(ObjectiveStatus.RESOLVED, phase)
        self.assertEqual(ObjectiveStatus.RESOLVED, objective_after.status if objective_after else None)

    def test_update_objective_phase_ignores_failed_review_remediation_superseded_by_completed_peer(self) -> None:
        project = Project(
            id=new_id("project"),
            name="phase-project",
            description="Objective phase persistence",
            adapter_name="generic",
        )
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Objective phase",
            summary="A failed duplicate remediation should not keep the objective paused.",
            status=ObjectiveStatus.PLANNING,
        )
        self.store.create_objective(objective)
        completed_ref_id = f"{objective.id}:review_1:unit_test_coverage"
        failed_ref_id = f"{objective.id}:review_2:unit_test_coverage"
        self.store.create_task(
            Task(
                id=new_id("task"),
                project_id=project.id,
                objective_id=objective.id,
                title="Completed review remediation",
                objective="Produce the review packet.",
                status=TaskStatus.COMPLETED,
                external_ref_type="objective_review",
                external_ref_id=completed_ref_id,
                external_ref_metadata={"objective_review_remediation": {"dimension": "unit_test_coverage"}},
            )
        )
        self.store.create_task(
            Task(
                id=new_id("task"),
                project_id=project.id,
                objective_id=objective.id,
                title="Failed duplicate remediation",
                objective="Retry the same review packet.",
                status=TaskStatus.FAILED,
                external_ref_type="objective_review",
                external_ref_id=failed_ref_id,
                external_ref_metadata={"objective_review_remediation": {"dimension": "unit_test_coverage"}},
            )
        )

        phase = self.store.update_objective_phase(objective.id)
        objective_after = self.store.get_objective(objective.id)

        self.assertEqual(ObjectiveStatus.RESOLVED, phase)
        self.assertEqual(ObjectiveStatus.RESOLVED, objective_after.status if objective_after else None)

    def test_review_readiness_ignores_failed_review_remediation_superseded_by_completed_peer(self) -> None:
        project = Project(
            id=new_id("project"),
            name="review-project",
            description="Review readiness",
            adapter_name="generic",
        )
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Review readiness",
            summary="A failed duplicate remediation should not block promotion review.",
            status=ObjectiveStatus.RESOLVED,
        )
        self.store.create_objective(objective)
        completed_ref_id = f"{objective.id}:review_1:unit_test_coverage"
        failed_ref_id = f"{objective.id}:review_2:unit_test_coverage"
        completed = Task(
            id=new_id("task"),
            project_id=project.id,
            objective_id=objective.id,
            title="Completed review remediation",
            objective="Produce the review packet.",
            status=TaskStatus.COMPLETED,
            external_ref_type="objective_review",
            external_ref_id=completed_ref_id,
            external_ref_metadata={"objective_review_remediation": {"dimension": "unit_test_coverage"}},
        )
        failed = Task(
            id=new_id("task"),
            project_id=project.id,
            objective_id=objective.id,
            title="Failed duplicate remediation",
            objective="Retry the same review packet.",
            status=TaskStatus.FAILED,
            external_ref_type="objective_review",
            external_ref_id=failed_ref_id,
            external_ref_metadata={"objective_review_remediation": {"dimension": "unit_test_coverage"}},
        )
        self.store.create_task(completed)
        self.store.create_task(failed)

        readiness = WorkflowService(self.store).review_readiness(objective.id)

        self.assertTrue(readiness.ready)
        failed_check = next(check for check in readiness.checks if check["key"] == "no_unresolved_failed_tasks")
        self.assertTrue(failed_check["ok"])

    def test_concurrent_initialize_serializes_pending_migrations(self) -> None:
        db_path = Path(self.temp_dir.name) / "concurrent-harness.db"
        errors: list[Exception] = []
        started = threading.Barrier(2)

        def initialize_store() -> None:
            store = SQLiteHarnessStore(db_path)
            with store.connect() as connection:
                connection.create_function("py_sleep", 1, time.sleep)
                started.wait(timeout=5)
                try:
                    apply_migrations(connection)
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

        with mock.patch(
            "accruvia_harness.migrations.MIGRATIONS",
            [Migration(version=1, name="slow_init", sql="SELECT py_sleep(0.2);")],
        ):
            threads = [threading.Thread(target=initialize_store) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual([], errors)
        self.assertEqual(1, SQLiteHarnessStore(db_path).schema_version())

    def test_project_round_trip_preserves_adapter_name(self) -> None:
        project = Project(
            id=new_id("project"),
            name="adapter-project",
            description="Project adapter persistence",
            adapter_name="private_repo",
        )
        self.store.create_project(project)

        loaded = self.store.get_project(project.id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual("private_repo", loaded.adapter_name)

    def test_control_plane_bootstraps_default_state(self) -> None:
        system = self.store.get_control_system_state()
        lanes = {lane.lane_name: lane.state for lane in self.store.list_control_lane_states()}

        self.assertEqual(GlobalSystemState.OFF, system.global_state)
        self.assertFalse(system.master_switch)
        self.assertEqual(
            {
                "api": ControlLaneStateValue.PAUSED,
                "harness": ControlLaneStateValue.PAUSED,
                "telegram": ControlLaneStateValue.PAUSED,
                "watch": ControlLaneStateValue.PAUSED,
                "worker": ControlLaneStateValue.PAUSED,
            },
            lanes,
        )

    def test_control_plane_service_tracks_state_transitions(self) -> None:
        control_plane = ControlPlane(self.store)

        started = control_plane.turn_on()
        frozen = control_plane.freeze("smoke")
        thawed = control_plane.thaw()
        stopped = control_plane.turn_off()

        self.assertIn(started["global_state"], {"starting", "healthy"})
        self.assertTrue(started["master_switch"])
        self.assertEqual("frozen", frozen["global_state"])
        self.assertEqual("smoke", frozen["frozen_reason"])
        self.assertIn(thawed["global_state"], {"starting", "healthy"})
        self.assertEqual("off", stopped["global_state"])
        self.assertFalse(stopped["master_switch"])

    def test_control_plane_persists_events_breadcrumbs_and_recovery_actions(self) -> None:
        event = ControlEvent(
            id=new_id("control_event"),
            event_type="provider_degraded",
            entity_type="lane",
            entity_id="worker",
            producer="test",
            payload={"class": "provider_rate_limit"},
            idempotency_key=new_id("event_key"),
        )
        breadcrumb = ControlBreadcrumb(
            id=new_id("breadcrumb"),
            entity_type="task",
            entity_id="task_123",
            worker_run_id="run_123",
            classification="timeout",
            path="/tmp/breadcrumb",
        )
        recovery = ControlRecoveryAction(
            id=new_id("recovery"),
            action_type="restart",
            target_type="lane",
            target_id="worker",
            reason="hung_process",
            result="applied",
        )

        self.store.create_control_event(event)
        self.store.create_control_breadcrumb(breadcrumb)
        self.store.create_control_recovery_action(recovery)

        loaded_events = self.store.list_control_events(entity_type="lane", entity_id="worker")
        loaded_breadcrumbs = self.store.list_control_breadcrumbs(entity_type="task", entity_id="task_123")
        loaded_actions = self.store.list_control_recovery_actions(target_type="lane", target_id="worker")

        self.assertEqual("provider_rate_limit", loaded_events[0].payload["class"])
        self.assertEqual("/tmp/breadcrumb", loaded_breadcrumbs[0].path)
        self.assertEqual("hung_process", loaded_actions[0].reason)

    def test_event_round_trip_preserves_payload(self) -> None:
        event = Event(
            id=new_id("event"),
            entity_type="task",
            entity_id="task_123",
            event_type="task_created",
            payload={"max_attempts": 3, "required_artifacts": ["plan", "report"]},
        )
        self.store.create_event(event)
        loaded = self.store.list_events(entity_type="task", entity_id="task_123")
        self.assertEqual(1, len(loaded))
        self.assertEqual("task_created", loaded[0].event_type)
        self.assertEqual(["plan", "report"], loaded[0].payload["required_artifacts"])

    def test_objective_context_round_trip(self) -> None:
        project = Project(id=new_id("project"), name="context-project", description="Context")
        self.store.create_project(project)
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title="Clarify workflow",
            summary="Need better intent capture",
        )
        self.store.create_objective(objective)
        intent = IntentModel(
            id=new_id("intent"),
            objective_id=objective.id,
            version=1,
            intent_summary="Capture operator intent before execution",
            non_negotiables=["No silent drift"],
            frustration_signals=["Repeated restarts"],
        )
        self.store.create_intent_model(intent)
        mermaid = MermaidArtifact(
            id=new_id("diagram"),
            objective_id=objective.id,
            diagram_type="workflow_control",
            version=1,
            status=MermaidStatus.FINISHED,
            summary="Accepted flow",
            content="flowchart TD\nA-->B",
            required_for_execution=True,
        )
        self.store.create_mermaid_artifact(mermaid)
        record = ContextRecord(
            id=new_id("context"),
            record_type="operator_comment",
            project_id=project.id,
            objective_id=objective.id,
            author_type="operator",
            author_id="shaun",
            content="This still feels wrong",
        )
        self.store.create_context_record(record)

        loaded_objective = self.store.get_objective(objective.id)
        loaded_intent = self.store.latest_intent_model(objective.id)
        loaded_mermaid = self.store.latest_mermaid_artifact(objective.id)
        loaded_records = self.store.list_context_records(objective_id=objective.id)

        self.assertEqual("Clarify workflow", loaded_objective.title if loaded_objective else None)
        self.assertEqual("Capture operator intent before execution", loaded_intent.intent_summary if loaded_intent else None)
        self.assertEqual(MermaidStatus.FINISHED, loaded_mermaid.status if loaded_mermaid else None)
        self.assertEqual("This still feels wrong", loaded_records[0].content)

    def test_task_leases_are_acquired_and_released(self) -> None:
        project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Lease me",
            objective="Test worker leasing",
        )
        self.store.create_task(task)

        leased = self.store.acquire_task_lease("worker-a", lease_seconds=60)

        self.assertIsNotNone(leased)
        leases = self.store.list_task_leases()
        self.assertEqual(1, len(leases))
        self.assertEqual(task.id, leases[0].task_id)
        self.assertEqual("worker-a", leases[0].worker_id)

        self.store.release_task_lease(task.id, "worker-a")
        self.assertEqual([], self.store.list_task_leases())

    def test_active_lease_blocks_second_worker_until_release(self) -> None:
        project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Lease me once",
            objective="Prevent duplicate acquisition",
        )
        self.store.create_task(task)

        first = self.store.acquire_task_lease("worker-a", lease_seconds=60)
        second = self.store.acquire_task_lease("worker-b", lease_seconds=60)

        self.assertEqual(task.id, first.id if first else None)
        self.assertIsNone(second)

        self.store.release_task_lease(task.id, "worker-a")
        third = self.store.acquire_task_lease("worker-b", lease_seconds=60)
        self.assertEqual(task.id, third.id if third else None)

    def test_update_task_external_metadata_round_trip(self) -> None:
        project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Metadata sync",
            objective="Persist issue metadata",
        )
        self.store.create_task(task)

        self.store.update_task_external_metadata(
            task.id,
            {"labels": ["bug", "triage"], "milestone": "MVP", "assignees": ["sanaani"]},
        )

        loaded = self.store.get_task(task.id)
        assert loaded is not None
        self.assertEqual(["bug", "triage"], loaded.external_ref_metadata["labels"])

    def test_metrics_snapshot_scopes_active_leases_by_project(self) -> None:
        project_a = Project(id=new_id("project"), name="a", description="A")
        project_b = Project(id=new_id("project"), name="b", description="B")
        self.store.create_project(project_a)
        self.store.create_project(project_b)
        task_a = Task(id=new_id("task"), project_id=project_a.id, title="A", objective="A")
        task_b = Task(id=new_id("task"), project_id=project_b.id, title="B", objective="B")
        self.store.create_task(task_a)
        self.store.create_task(task_b)
        self.store.acquire_task_lease("worker-a", lease_seconds=60, project_id=project_a.id)
        self.store.acquire_task_lease("worker-b", lease_seconds=60, project_id=project_b.id)

        metrics_a = self.store.metrics_snapshot(project_a.id)
        metrics_b = self.store.metrics_snapshot(project_b.id)

        self.assertEqual(1, metrics_a["active_leases"])
        self.assertEqual(1, metrics_b["active_leases"])

    def test_recover_stale_state_resets_runs_tasks_and_expired_leases(self) -> None:
        project = Project(id=new_id("project"), name="recover", description="Recover")
        self.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Recover me",
            objective="Recover stale state",
            status=TaskStatus.ACTIVE,
        )
        self.store.create_task(task)
        run = Run(
            id=new_id("run"),
            task_id=task.id,
            status=RunStatus.WORKING,
            attempt=1,
            summary="working",
        )
        self.store.create_run(run)
        self.store.upsert_control_worker_run(ControlWorkerRun(id=run.id, task_id=task.id, status="started"))
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO task_leases (task_id, worker_id, lease_expires_at, created_at) VALUES (?, ?, ?, ?)",
                (task.id, "worker-a", "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"),
            )

        recovered = self.store.recover_stale_state()
        task_after = self.store.get_task(task.id)
        run_after = self.store.get_run(run.id)
        control_run_after = self.store.get_control_worker_run(run.id)

        self.assertEqual(1, recovered["runs"])
        self.assertEqual(1, recovered["tasks"])
        self.assertEqual(1, recovered["leases"])
        self.assertEqual(TaskStatus.PENDING, task_after.status if task_after else None)
        self.assertEqual(RunStatus.FAILED, run_after.status if run_after else None)
        self.assertEqual("failed", control_run_after.status if control_run_after else None)
        self.assertEqual("system_failure", control_run_after.classification if control_run_after else None)

    def test_recover_stale_state_preserves_in_progress_run_with_active_lease(self) -> None:
        project = Project(id=new_id("project"), name="recover-live", description="Recover live")
        self.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Still running",
            objective="Do not recover a live run",
            status=TaskStatus.ACTIVE,
        )
        self.store.create_task(task)
        run = Run(
            id=new_id("run"),
            task_id=task.id,
            status=RunStatus.WORKING,
            attempt=1,
            summary="working",
        )
        self.store.create_run(run)
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO task_leases (task_id, worker_id, lease_expires_at, created_at) VALUES (?, ?, ?, ?)",
                (task.id, "worker-a", "2099-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
            )

        recovered = self.store.recover_stale_state()
        task_after = self.store.get_task(task.id)
        run_after = self.store.get_run(run.id)

        self.assertEqual(0, recovered["runs"])
        self.assertEqual(0, recovered["tasks"])
        self.assertEqual(TaskStatus.ACTIVE, task_after.status if task_after else None)
        self.assertEqual(RunStatus.WORKING, run_after.status if run_after else None)

    def test_foreign_keys_are_enforced(self) -> None:
        task = Task(id=new_id("task"), project_id="missing-project", title="bad", objective="bad")

        with self.assertRaises(Exception):
            self.store.create_task(task)

    def test_invalid_task_transition_raises(self) -> None:
        project = Project(id=new_id("project"), name="transitions", description="Transitions")
        self.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Done",
            objective="done",
            status=TaskStatus.COMPLETED,
        )
        self.store.create_task(task)

        with self.assertRaises(ValueError):
            self.store.update_task_status(task.id, TaskStatus.ACTIVE)

    def test_create_evaluation_round_trip_uses_verdict_value(self) -> None:
        project = Project(id=new_id("project"), name="evals", description="evals")
        self.store.create_project(project)
        task = Task(id=new_id("task"), project_id=project.id, title="t", objective="o")
        self.store.create_task(task)
        run = Run(id=new_id("run"), task_id=task.id, status=RunStatus.COMPLETED, attempt=1, summary="done")
        self.store.create_run(run)
        evaluation = Evaluation(
            id=new_id("evaluation"),
            run_id=run.id,
            verdict=EvaluationVerdict.ACCEPTABLE,
            confidence=0.9,
            summary="ok",
            details={},
        )

        self.store.create_evaluation(evaluation)
        loaded = self.store.list_evaluations(run.id)[0]

        self.assertEqual(EvaluationVerdict.ACCEPTABLE, loaded.verdict)

    def test_validation_queue_table_exists_after_init(self) -> None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='validation_queue'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("validation_queue", row["name"])

    def test_decision_queue_table_exists_after_init(self) -> None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='decision_queue'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("decision_queue", row["name"])

    def test_corrupt_task_json_falls_back_instead_of_crashing(self) -> None:
        project = Project(id=new_id("project"), name="corrupt", description="Corrupt")
        self.store.create_project(project)
        task = Task(
            id=new_id("task"),
            project_id=project.id,
            title="Corrupt metadata",
            objective="Fallback on bad json",
            external_ref_metadata={"labels": ["bug"]},
            required_artifacts=["plan", "report"],
        )
        self.store.create_task(task)
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE tasks SET external_ref_metadata_json = ?, required_artifacts_json = ? WHERE id = ?",
                ("{bad", "[oops", task.id),
            )

        loaded = self.store.get_task(task.id)

        self.assertEqual({}, loaded.external_ref_metadata if loaded else None)
        self.assertEqual([], loaded.required_artifacts if loaded else None)
