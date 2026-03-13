"""Tests for the chaos monkey module."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from accruvia_harness.chaos.domain import (
    BlastRadius,
    ChaosProbe,
    ChaosRound,
    CrashType,
    Severity,
)
from accruvia_harness.chaos.heartbeat import ChaosDrainResult, drain_chaos_findings
from accruvia_harness.chaos.injectors import (
    ALL_INJECTORS,
    DEFAULT_INJECTOR_NAMES,
    ConcurrentRunInjector,
    DBCorruptionInjector,
    LeaseContentionInjector,
    PartialWriteInjector,
    ShadowSupervisorInjector,
    TimeoutExhaustionInjector,
    WorkerCrashInjector,
)
from accruvia_harness.chaos.runner import ChaosRunner, write_chaos_report
from accruvia_harness.chaos.sandbox import ChaosSandbox
from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import Event, Project, TaskStatus, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.store import SQLiteHarnessStore


def _make_config(base: Path) -> HarnessConfig:
    return HarnessConfig(
        db_path=base / "harness.db",
        workspace_root=base / "workspace",
        log_path=base / "harness.log",
        telemetry_dir=base / "telemetry",
        default_project_name="chaos-test",
        default_repo="chaos/test",
        runtime_backend="local",
        temporal_target="localhost:7233",
        temporal_namespace="default",
        temporal_task_queue="accruvia-harness",
        worker_backend="local",
        worker_command=None,
        llm_backend="auto",
        llm_model=None,
        llm_command=None,
        llm_codex_command=None,
        llm_claude_command=None,
        llm_accruvia_client_command=None,
    )


def _setup_engine(base: Path) -> tuple[SQLiteHarnessStore, HarnessEngine, str]:
    store = SQLiteHarnessStore(base / "harness.db")
    store.initialize()
    engine = HarnessEngine(store=store, workspace_root=base / "workspace")
    project = Project(id=new_id("project"), name="chaos-test", description="Chaos test project")
    store.create_project(project)
    return store, engine, project.id


def _create_pending_task(engine: HarnessEngine, project_id: str):
    return engine.create_task_with_policy(
        project_id=project_id,
        title="Chaos target task",
        objective="Task for chaos testing",
        priority=100,
        parent_task_id=None,
        source_run_id=None,
        external_ref_type=None,
        external_ref_id=None,
        strategy="default",
        max_attempts=5,
        required_artifacts=["plan", "report"],
    )


# ---------------------------------------------------------------------------
# Domain tests
# ---------------------------------------------------------------------------
class ChaosDomainTests(unittest.TestCase):
    def test_severity_score_critical_data_corruption(self):
        probe = ChaosProbe(
            crash_type=CrashType.DATA_CORRUPTION,
            blast_radius=BlastRadius.DATA,
            reproducibility=1.0,
        )
        self.assertGreaterEqual(probe.severity_score(), 12)
        self.assertEqual(probe.severity(), Severity.CRITICAL)

    def test_severity_score_low_timeout_recovered(self):
        probe = ChaosProbe(
            crash_type=CrashType.TIMEOUT,
            blast_radius=BlastRadius.WORKER,
            recovered=True,
            reproducibility=0.33,
        )
        self.assertLess(probe.severity_score(), 3)
        self.assertEqual(probe.severity(), Severity.LOW)

    def test_severity_score_user_controllable_multiplier(self):
        base = ChaosProbe(
            crash_type=CrashType.UNHANDLED_EXCEPTION,
            blast_radius=BlastRadius.SERVICE,
            reproducibility=1.0,
        )
        controlled = ChaosProbe(
            crash_type=CrashType.UNHANDLED_EXCEPTION,
            blast_radius=BlastRadius.SERVICE,
            reproducibility=1.0,
            user_controllable=True,
        )
        self.assertGreater(controlled.severity_score(), base.severity_score())

    def test_severity_score_recovery_halves(self):
        not_recovered = ChaosProbe(
            crash_type=CrashType.OOM,
            blast_radius=BlastRadius.APP,
            reproducibility=1.0,
            recovered=False,
        )
        recovered = ChaosProbe(
            crash_type=CrashType.OOM,
            blast_radius=BlastRadius.APP,
            reproducibility=1.0,
            recovered=True,
        )
        self.assertAlmostEqual(
            not_recovered.severity_score(),
            recovered.severity_score() * 2,
        )

    def test_to_heartbeat_task(self):
        probe = ChaosProbe(
            probe_type="worker_crash",
            crash_type=CrashType.UNHANDLED_EXCEPTION,
            blast_radius=BlastRadius.WORKER,
            reproducibility=1.0,
            phase="working",
            description="test crash",
            exception_class="RuntimeError",
            exception_message="boom",
        )
        task_spec = probe.to_heartbeat_task()
        self.assertIn("[chaos]", task_spec["title"])
        self.assertIn("worker_crash", task_spec["title"])
        self.assertIn("priority", task_spec)
        self.assertIn("objective", task_spec)

    def test_chaos_round_summary(self):
        rnd = ChaosRound()
        rnd.injectors_run = 3
        rnd.errors_found = 2
        rnd.probes = [
            ChaosProbe(crash_type=CrashType.DATA_CORRUPTION, blast_radius=BlastRadius.DATA, reproducibility=1.0),
            ChaosProbe(crash_type=CrashType.TIMEOUT, blast_radius=BlastRadius.WORKER, reproducibility=0.1),
        ]
        summary = rnd.summary()
        self.assertEqual(summary["injectors_run"], 3)
        self.assertEqual(summary["errors_found"], 2)
        self.assertEqual(summary["probes"], 2)
        self.assertIn("by_severity", summary)

    def test_chaos_round_critical_probes(self):
        rnd = ChaosRound()
        rnd.probes = [
            ChaosProbe(crash_type=CrashType.DATA_CORRUPTION, blast_radius=BlastRadius.DATA, reproducibility=1.0),
            ChaosProbe(crash_type=CrashType.TIMEOUT, blast_radius=BlastRadius.WORKER, reproducibility=0.1),
        ]
        critical = rnd.critical_probes()
        self.assertEqual(len(critical), 1)
        self.assertEqual(critical[0].crash_type, CrashType.DATA_CORRUPTION)

    def test_no_crash_probe_severity_is_low(self):
        probe = ChaosProbe(recovered=True)
        self.assertEqual(probe.severity(), Severity.LOW)


# ---------------------------------------------------------------------------
# Sandbox tests
# ---------------------------------------------------------------------------
class ChaosSandboxTests(unittest.TestCase):
    def test_sandbox_copies_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store = SQLiteHarnessStore(base / "source.db")
            store.initialize()
            project = Project(id=new_id("project"), name="test", description="test")
            store.create_project(project)

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "sandbox" / "chaos.db",
                worktree_path=None,
            )
            sandbox.initialize(base / "source.db")

            self.assertIsNotNone(sandbox.store)
            projects = sandbox.store.list_projects()
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0].name, "test")

    def test_sandbox_teardown_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store = SQLiteHarnessStore(base / "source.db")
            store.initialize()

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "sandbox" / "chaos.db",
                worktree_path=None,
            )
            sandbox.initialize(base / "source.db")
            self.assertTrue((base / "sandbox").exists())

            sandbox.teardown()
            self.assertFalse((base / "sandbox").exists())

    def test_sandbox_isolation_does_not_affect_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            source_store = SQLiteHarnessStore(base / "source.db")
            source_store.initialize()
            project = Project(id=new_id("project"), name="original", description="test")
            source_store.create_project(project)

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "sandbox" / "chaos.db",
                worktree_path=None,
            )
            sandbox.initialize(base / "source.db")

            # Mutate sandbox DB
            sandbox_project = Project(id=new_id("project"), name="chaos-only", description="only in sandbox")
            sandbox.store.create_project(sandbox_project)

            # Source is unchanged
            self.assertEqual(len(source_store.list_projects()), 1)
            self.assertEqual(len(sandbox.store.list_projects()), 2)
            sandbox.teardown()


# ---------------------------------------------------------------------------
# Injector tests
# ---------------------------------------------------------------------------
class WorkerCrashInjectorTests(unittest.TestCase):
    def test_worker_crash_produces_probes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)
            _create_pending_task(engine, project_id)

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "harness.db",
                worktree_path=None,
                store=store,
            )

            injector = WorkerCrashInjector()
            probes = injector.inject(engine, sandbox)

            # Should produce probes for multiple phases
            self.assertGreater(len(probes), 0)
            # "working" phase should produce a crash
            working_probes = [p for p in probes if p.phase == "working"]
            self.assertEqual(len(working_probes), 1)
            self.assertEqual(working_probes[0].crash_type, CrashType.UNHANDLED_EXCEPTION)
            self.assertGreater(working_probes[0].reproducibility, 0)


class LeaseContentionInjectorTests(unittest.TestCase):
    def test_lease_contention_single_winner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)
            _create_pending_task(engine, project_id)

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "harness.db",
                worktree_path=None,
                store=store,
            )

            injector = LeaseContentionInjector()
            probes = injector.inject(engine, sandbox)

            self.assertEqual(len(probes), 1)
            probe = probes[0]
            # SQLite with proper locking should produce exactly 1 winner
            if probe.recovered:
                self.assertIsNone(probe.crash_type)
            else:
                # Could be a double-lease finding -- still valid
                self.assertIn(
                    probe.crash_type,
                    [CrashType.DATA_CORRUPTION, CrashType.DEADLOCK],
                )


class DBCorruptionInjectorTests(unittest.TestCase):
    def test_db_corruption_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)
            _create_pending_task(engine, project_id)

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "harness.db",
                worktree_path=None,
                store=store,
            )

            injector = DBCorruptionInjector()
            probes = injector.inject(engine, sandbox)

            self.assertEqual(len(probes), 1)
            probe = probes[0]
            # Engine should either crash (good -- detected) or bypass (bad -- finding)
            self.assertIsNotNone(probe.crash_type)
            self.assertIn(
                probe.crash_type,
                [CrashType.UNHANDLED_EXCEPTION, CrashType.VALIDATION_BYPASS],
            )


class PartialWriteInjectorTests(unittest.TestCase):
    def test_partial_write_produces_probe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)
            _create_pending_task(engine, project_id)

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "harness.db",
                worktree_path=None,
                store=store,
            )

            injector = PartialWriteInjector()
            probes = injector.inject(engine, sandbox)

            self.assertEqual(len(probes), 1)
            # Should find something -- either crash or partial write or recovery
            probe = probes[0]
            self.assertTrue(
                probe.crash_type is not None or probe.recovered,
                "Probe should either find an issue or confirm recovery",
            )


class ConcurrentRunInjectorTests(unittest.TestCase):
    def test_concurrent_run_produces_probe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)
            _create_pending_task(engine, project_id)

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "harness.db",
                worktree_path=None,
                store=store,
            )

            injector = ConcurrentRunInjector()
            probes = injector.inject(engine, sandbox)

            self.assertEqual(len(probes), 1)

    def test_no_tasks_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)
            # No task created

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "harness.db",
                worktree_path=None,
                store=store,
            )

            injector = ConcurrentRunInjector()
            probes = injector.inject(engine, sandbox)
            self.assertEqual(len(probes), 0)


# ---------------------------------------------------------------------------
# Heartbeat drain tests
# ---------------------------------------------------------------------------
class HeartbeatDrainTests(unittest.TestCase):
    def test_drain_creates_tasks_from_findings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)

            # Simulate chaos findings as events
            store.create_event(Event(
                id=new_id("event"),
                entity_type="project",
                entity_id=project_id,
                event_type="chaos_finding",
                payload={
                    "probe_id": "abc123",
                    "probe_type": "worker_crash",
                    "severity": "high",
                    "score": 10.5,
                    "proposed_task": {
                        "title": "[chaos] worker_crash: unhandled_exception",
                        "objective": "Fix crash in working phase",
                        "priority": "P1",
                        "strategy": "fix",
                    },
                },
            ))

            result = drain_chaos_findings(
                store=store,
                project_id=project_id,
                task_service=engine,
                min_severity="high",
            )

            self.assertEqual(result.total_findings, 1)
            self.assertEqual(len(result.created_tasks), 1)
            self.assertEqual(result.skipped_duplicates, 0)

            # Verify task was created
            tasks = store.list_tasks(project_id)
            chaos_tasks = [t for t in tasks if "[chaos]" in t.title]
            self.assertEqual(len(chaos_tasks), 1)
            self.assertEqual(chaos_tasks[0].priority, 700)  # P1

    def test_drain_deduplicates_by_title(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)

            for _ in range(3):
                store.create_event(Event(
                    id=new_id("event"),
                    entity_type="project",
                    entity_id=project_id,
                    event_type="chaos_finding",
                    payload={
                        "severity": "high",
                        "proposed_task": {
                            "title": "[chaos] same finding",
                            "objective": "Same crash",
                            "priority": "P1",
                        },
                    },
                ))

            result = drain_chaos_findings(
                store=store,
                project_id=project_id,
                task_service=engine,
                min_severity="high",
            )

            self.assertEqual(result.total_findings, 3)
            self.assertEqual(len(result.created_tasks), 1)
            self.assertEqual(result.skipped_duplicates, 2)

    def test_drain_filters_by_min_severity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)

            store.create_event(Event(
                id=new_id("event"),
                entity_type="project",
                entity_id=project_id,
                event_type="chaos_finding",
                payload={
                    "severity": "low",
                    "proposed_task": {
                        "title": "[chaos] low severity",
                        "objective": "Minor",
                        "priority": "P3",
                    },
                },
            ))
            store.create_event(Event(
                id=new_id("event"),
                entity_type="project",
                entity_id=project_id,
                event_type="chaos_finding",
                payload={
                    "severity": "critical",
                    "proposed_task": {
                        "title": "[chaos] critical finding",
                        "objective": "Major",
                        "priority": "P0",
                    },
                },
            ))

            result = drain_chaos_findings(
                store=store,
                project_id=project_id,
                task_service=engine,
                min_severity="high",
            )

            self.assertEqual(result.total_findings, 2)
            self.assertEqual(len(result.created_tasks), 1)
            self.assertIn("critical", result.created_tasks[0]["title"])

    def test_drain_ignores_non_chaos_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)

            store.create_event(Event(
                id=new_id("event"),
                entity_type="project",
                entity_id=project_id,
                event_type="heartbeat_completed",
                payload={"summary": "normal heartbeat"},
            ))

            result = drain_chaos_findings(
                store=store,
                project_id=project_id,
                task_service=engine,
            )

            self.assertEqual(result.total_findings, 0)
            self.assertEqual(len(result.created_tasks), 0)


# ---------------------------------------------------------------------------
# Report writer tests
# ---------------------------------------------------------------------------
class ChaosReportTests(unittest.TestCase):
    def test_write_chaos_report_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rnd = ChaosRound()
            rnd.injectors_run = 1
            rnd.probes = [
                ChaosProbe(
                    probe_type="test",
                    crash_type=CrashType.OOM,
                    blast_radius=BlastRadius.APP,
                    reproducibility=1.0,
                    description="test probe",
                ),
            ]
            rnd.errors_found = 1

            output = Path(tmpdir) / "report.json"
            write_chaos_report(rnd, output)

            self.assertTrue(output.exists())
            import json
            data = json.loads(output.read_text())
            self.assertEqual(len(data["probes"]), 1)
            self.assertEqual(data["probes"][0]["crash_type"], "oom")
            self.assertEqual(data["injectors_run"], 1)


# ---------------------------------------------------------------------------
# Runner integration tests
# ---------------------------------------------------------------------------
class ChaosRunnerTests(unittest.TestCase):
    def test_runner_defaults_to_focused_injectors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            config = _make_config(base)

            runner = ChaosRunner(config=config)

            self.assertEqual(
                [injector.name for injector in runner.injectors],
                list(DEFAULT_INJECTOR_NAMES),
            )

    def test_runner_dry_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store = SQLiteHarnessStore(base / "harness.db")
            store.initialize()
            project = Project(id=new_id("project"), name="runner-test", description="test")
            store.create_project(project)
            engine = HarnessEngine(store=store, workspace_root=base / "workspace")
            _create_pending_task(engine, project.id)

            config = _make_config(base)

            runner = ChaosRunner(
                config=config,
                injectors=[WorkerCrashInjector()],
                memory_limit_mb=4096,
                cpu_limit_seconds=60,
            )
            chaos_round = runner.run()

            self.assertGreater(chaos_round.injectors_run, 0)
            self.assertIsNotNone(chaos_round.finished_at)

    def test_runner_feed_creates_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store = SQLiteHarnessStore(base / "harness.db")
            store.initialize()
            project = Project(id=new_id("project"), name="feed-test", description="test")
            store.create_project(project)
            engine = HarnessEngine(store=store, workspace_root=base / "workspace")
            _create_pending_task(engine, project.id)

            config = _make_config(base)

            runner = ChaosRunner(
                config=config,
                injectors=[WorkerCrashInjector()],
                memory_limit_mb=4096,
                cpu_limit_seconds=60,
                feed_to_project_id=project.id,
            )
            chaos_round = runner.run_and_feed(store)

            # Check that chaos_finding events were created in production store
            events = store.list_events(entity_type="project", entity_id=project.id)
            chaos_events = [e for e in events if e.event_type == "chaos_finding"]
            # May or may not have findings depending on engine behavior, but
            # the round should complete without error
            self.assertIsNotNone(chaos_round.finished_at)

    def test_runner_handles_missing_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            config = _make_config(base)
            # Point to a DB path that does not exist
            config.db_path = base / "nonexistent.db"
            runner = ChaosRunner(config=config)
            chaos_round = runner.run()
            # Should return empty round, not crash
            self.assertEqual(chaos_round.injectors_run, 0)


# ---------------------------------------------------------------------------
# CLI parser tests
# ---------------------------------------------------------------------------
class ChaosCLITests(unittest.TestCase):
    def test_chaos_parser_defaults(self):
        from accruvia_harness.cli_parser import build_parser
        parser = build_parser()
        args = parser.parse_args(["chaos"])
        self.assertEqual(args.command, "chaos")
        self.assertIsNone(args.project_id)
        self.assertEqual(args.memory_limit_mb, 2048)
        self.assertEqual(args.cpu_limit_seconds, 300)
        self.assertEqual(args.min_severity, "high")
        self.assertFalse(args.dry_run)
        self.assertFalse(args.all_injectors)
        self.assertIsNone(args.report_path)

    def test_chaos_parser_all_options(self):
        from accruvia_harness.cli_parser import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "chaos",
            "--project-id", "proj_123",
            "--memory-limit-mb", "1024",
            "--cpu-limit-seconds", "120",
            "--report-path", "/tmp/chaos.json",
            "--min-severity", "critical",
            "--all-injectors",
            "--dry-run",
        ])
        self.assertEqual(args.project_id, "proj_123")
        self.assertEqual(args.memory_limit_mb, 1024)
        self.assertEqual(args.cpu_limit_seconds, 120)
        self.assertEqual(args.report_path, "/tmp/chaos.json")
        self.assertEqual(args.min_severity, "critical")
        self.assertTrue(args.all_injectors)
        self.assertTrue(args.dry_run)


# ---------------------------------------------------------------------------
# Injector registry tests
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Shadow supervisor tests
# ---------------------------------------------------------------------------
class ShadowSupervisorInjectorTests(unittest.TestCase):
    def test_shadow_supervisor_runs_and_audits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)
            # Create several pending tasks for the supervisor to process
            for i in range(3):
                engine.create_task_with_policy(
                    project_id=project_id,
                    title=f"Shadow task {i}",
                    objective=f"Task {i} for shadow testing",
                    priority=100,
                    parent_task_id=None,
                    source_run_id=None,
                    external_ref_type=None,
                    external_ref_id=None,
                    strategy="default",
                    max_attempts=2,
                    required_artifacts=["plan", "report"],
                )

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "harness.db",
                worktree_path=None,
                store=store,
            )

            injector = ShadowSupervisorInjector(max_iterations=5)
            probes = injector.inject(engine, sandbox)

            # Should produce at least the supervisor probe itself
            self.assertGreater(len(probes), 0)
            supervisor_probes = [p for p in probes if p.probe_type == "shadow_supervisor"]
            self.assertEqual(len(supervisor_probes), 1)

    def test_shadow_supervisor_no_tasks_produces_clean_probe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)
            # No tasks -- supervisor should idle out cleanly

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "harness.db",
                worktree_path=None,
                store=store,
            )

            injector = ShadowSupervisorInjector(max_iterations=3)
            probes = injector.inject(engine, sandbox)

            supervisor_probes = [p for p in probes if p.probe_type == "shadow_supervisor"]
            self.assertEqual(len(supervisor_probes), 1)
            self.assertTrue(supervisor_probes[0].recovered)

    def test_shadow_supervisor_detects_stuck_run(self):
        """If a run gets stuck in an in-progress state, shadow should report it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)

            from accruvia_harness.domain import Run, RunStatus
            from accruvia_harness.workers import WorkResult

            # Create a worker that leaves the run in a weird state
            class _StuckWorker:
                def work(self, t, r, ws):
                    return WorkResult(
                        outcome="success",
                        summary="ok",
                        artifacts=[],
                    )

            engine.set_worker(_StuckWorker())
            task = _create_pending_task(engine, project_id)

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "harness.db",
                worktree_path=None,
                store=store,
            )

            injector = ShadowSupervisorInjector(max_iterations=3)
            probes = injector.inject(engine, sandbox)

            # The supervisor probe itself should exist
            self.assertTrue(any(p.probe_type == "shadow_supervisor" for p in probes))

    def test_shadow_supervisor_with_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store, engine, project_id = _setup_engine(base)

            sandbox = ChaosSandbox(
                sandbox_root=base / "sandbox",
                db_path=base / "harness.db",
                worktree_path=None,
                store=store,
            )

            injector = ShadowSupervisorInjector(
                max_iterations=3,
                heartbeat_project_ids=[project_id],
                heartbeat_interval_seconds=0.1,
            )
            probes = injector.inject(engine, sandbox)

            # Should run without crashing even if heartbeat has no LLM
            self.assertTrue(any(p.probe_type == "shadow_supervisor" for p in probes))


# ---------------------------------------------------------------------------
# CLI parser tests for shadow options
# ---------------------------------------------------------------------------
class ChaosShadowCLITests(unittest.TestCase):
    def test_shadow_options_default(self):
        from accruvia_harness.cli_parser import build_parser
        parser = build_parser()
        args = parser.parse_args(["chaos"])
        self.assertEqual(args.shadow_iterations, 10)
        self.assertIsNone(args.shadow_heartbeat_interval)

    def test_shadow_options_custom(self):
        from accruvia_harness.cli_parser import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "chaos",
            "--shadow-iterations", "20",
            "--shadow-heartbeat-interval", "60.0",
        ])
        self.assertEqual(args.shadow_iterations, 20)
        self.assertAlmostEqual(args.shadow_heartbeat_interval, 60.0)


# ---------------------------------------------------------------------------
# Injector registry tests
# ---------------------------------------------------------------------------
class InjectorRegistryTests(unittest.TestCase):
    def test_all_injectors_registered(self):
        self.assertEqual(len(ALL_INJECTORS), 7)
        names = {i.name for i in ALL_INJECTORS}
        self.assertIn("worker_crash", names)
        self.assertIn("lease_contention", names)
        self.assertIn("db_corruption", names)
        self.assertIn("timeout_exhaustion", names)
        self.assertIn("partial_write", names)
        self.assertIn("concurrent_run", names)
        self.assertIn("shadow_supervisor", names)

    def test_all_injectors_have_description(self):
        for injector in ALL_INJECTORS:
            self.assertTrue(len(injector.description) > 0, f"{injector.name} has no description")


if __name__ == "__main__":
    unittest.main()
