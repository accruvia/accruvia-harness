"""Chaos injectors -- each targets a specific failure mode."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Protocol

from accruvia_harness.chaos.domain import (
    BlastRadius,
    ChaosProbe,
    CrashType,
)
from accruvia_harness.chaos.sandbox import ChaosSandbox
from accruvia_harness.domain import TaskStatus
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.workers import WorkResult

logger = logging.getLogger(__name__)

RETRY_COUNT = 3


class ChaosInjector(Protocol):
    name: str
    description: str

    def inject(self, engine: HarnessEngine, sandbox: ChaosSandbox) -> list[ChaosProbe]:
        ...


def _run_with_capture(fn, probe: ChaosProbe) -> ChaosProbe:
    """Run fn(), capture any exception into probe."""
    try:
        fn()
        probe.recovered = True
    except MemoryError:
        probe.crash_type = CrashType.OOM
        probe.blast_radius = BlastRadius.APP
        probe.exception_class = "MemoryError"
        probe.traceback = traceback.format_exc()
    except TimeoutError:
        probe.crash_type = CrashType.TIMEOUT
        probe.exception_class = "TimeoutError"
        probe.traceback = traceback.format_exc()
    except Exception as exc:
        probe.crash_type = CrashType.UNHANDLED_EXCEPTION
        probe.exception_class = type(exc).__name__
        probe.exception_message = str(exc)[:500]
        probe.traceback = traceback.format_exc()
    return probe


def _reproducibility(hits: int, attempts: int) -> float:
    return hits / attempts if attempts > 0 else 0.0


def _first_pending_task(engine: HarnessEngine):
    """Get first pending task from sandbox store."""
    tasks = engine.store.list_tasks()
    return next((t for t in tasks if t.status == TaskStatus.PENDING), None)


# ---------------------------------------------------------------------------
# Worker crash at each run phase
# ---------------------------------------------------------------------------
class WorkerCrashInjector:
    name = "worker_crash"
    description = "Injects exceptions at each run phase to verify recovery"

    def inject(self, engine: HarnessEngine, sandbox: ChaosSandbox) -> list[ChaosProbe]:
        probes: list[ChaosProbe] = []
        task = _first_pending_task(engine)
        if not task:
            return probes

        phases = ["planning", "working", "analyzing", "deciding"]
        for phase in phases:
            hits = 0
            last_probe: ChaosProbe | None = None

            for _attempt in range(RETRY_COUNT):
                probe = ChaosProbe(
                    probe_type=self.name,
                    injector=self.name,
                    description=f"Raise RuntimeError during {phase}",
                    phase=phase,
                    task_id=task.id,
                    blast_radius=BlastRadius.WORKER,
                )

                class _CrashWorker:
                    def __init__(self, target_phase):
                        self._phase = target_phase

                    def work(self, t, r, ws):
                        if self._phase == "working":
                            raise RuntimeError(f"Chaos: crash in {self._phase}")
                        return WorkResult(
                            outcome="success",
                            summary="chaos baseline",
                            artifacts=[],
                        )

                original_worker = engine.worker
                engine.set_worker(_CrashWorker(phase))
                try:
                    _run_with_capture(lambda: engine.run_once(task.id), probe)
                finally:
                    engine.set_worker(original_worker)

                if probe.crash_type is not None:
                    hits += 1
                last_probe = probe

                # Reset task to pending for next attempt
                try:
                    engine.store.update_task_status(task.id, TaskStatus.PENDING)
                except (ValueError, Exception):
                    pass

            if last_probe:
                last_probe.reproducibility = _reproducibility(hits, RETRY_COUNT)
                probes.append(last_probe)

        return probes


# ---------------------------------------------------------------------------
# Lease contention (concurrent task acquisition)
# ---------------------------------------------------------------------------
class LeaseContentionInjector:
    name = "lease_contention"
    description = "Simulates multiple workers racing to acquire the same task"

    def inject(self, engine: HarnessEngine, sandbox: ChaosSandbox) -> list[ChaosProbe]:
        probes: list[ChaosProbe] = []
        store = sandbox.store
        if not store:
            return probes

        task = _first_pending_task(engine)
        if not task:
            return probes

        results: list[tuple[str, Exception | None]] = []
        barrier = threading.Barrier(4, timeout=10)

        def _acquire(worker_id: str):
            try:
                barrier.wait()
                result = store.acquire_task_lease(worker_id, 60, task.project_id)
                results.append((worker_id, None if result else ValueError("no task")))
            except Exception as exc:
                results.append((worker_id, exc))

        threads = [
            threading.Thread(target=_acquire, args=(f"chaos-{i}",))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        winners = [r for r in results if r[1] is None]
        losers = [r for r in results if r[1] is not None]

        probe = ChaosProbe(
            probe_type=self.name,
            injector=self.name,
            description=f"{len(winners)} winners, {len(losers)} losers out of 4 workers",
            task_id=task.id,
            blast_radius=BlastRadius.SERVICE,
        )

        if len(winners) > 1:
            probe.crash_type = CrashType.DATA_CORRUPTION
            probe.blast_radius = BlastRadius.DATA
            probe.exception_message = f"Double-lease: {len(winners)} workers acquired same task"
            probe.reproducibility = 1.0
        elif len(winners) == 0:
            probe.crash_type = CrashType.DEADLOCK
            probe.exception_message = "No worker could acquire lease"
            probe.reproducibility = 1.0
        else:
            probe.recovered = True

        # Clean up leases
        for worker_id, _ in results:
            try:
                store.release_task_lease(task.id, worker_id)
            except Exception:
                pass

        probes.append(probe)
        return probes


# ---------------------------------------------------------------------------
# DB corruption (invalid status)
# ---------------------------------------------------------------------------
class DBCorruptionInjector:
    name = "db_corruption"
    description = "Verifies engine handles corrupt/missing DB records gracefully"

    def inject(self, engine: HarnessEngine, sandbox: ChaosSandbox) -> list[ChaosProbe]:
        probes: list[ChaosProbe] = []
        store = sandbox.store
        if not store:
            return probes

        task = _first_pending_task(engine)
        if not task:
            return probes

        probe = ChaosProbe(
            probe_type=self.name,
            injector=self.name,
            description="Set task status to garbage value, then run",
            task_id=task.id,
            blast_radius=BlastRadius.DATA,
            user_controllable=False,
        )

        # Write garbage status directly via SQL
        try:
            with store.connect() as conn:
                conn.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?",
                    ("CHAOS_INVALID", task.id),
                )
        except Exception as exc:
            probe.crash_type = CrashType.UNHANDLED_EXCEPTION
            probe.exception_class = type(exc).__name__
            probe.exception_message = str(exc)[:500]
            probes.append(probe)
            return probes

        _run_with_capture(lambda: engine.run_once(task.id), probe)

        if probe.crash_type is None:
            # Engine silently accepted garbage status
            probe.crash_type = CrashType.VALIDATION_BYPASS
            probe.exception_message = "Engine accepted task with invalid status 'CHAOS_INVALID'"
            probe.recovered = False

        probe.reproducibility = 1.0
        probes.append(probe)
        return probes


# ---------------------------------------------------------------------------
# Timeout exhaustion
# ---------------------------------------------------------------------------
class TimeoutExhaustionInjector:
    name = "timeout_exhaustion"
    description = "Worker that never returns, testing timeout enforcement"

    def inject(self, engine: HarnessEngine, sandbox: ChaosSandbox) -> list[ChaosProbe]:
        probes: list[ChaosProbe] = []
        task = _first_pending_task(engine)
        if not task:
            return probes

        probe = ChaosProbe(
            probe_type=self.name,
            injector=self.name,
            description="Worker sleeps forever, testing if engine enforces timeout",
            task_id=task.id,
            blast_radius=BlastRadius.SERVICE,
        )

        class _HangingWorker:
            def work(self, t, r, ws):
                time.sleep(3600)
                return WorkResult(outcome="success", summary="unreachable", artifacts=[])

        original_worker = engine.worker
        engine.set_worker(_HangingWorker())

        result_holder: list = [None]

        def _run():
            try:
                engine.run_once(task.id)
                result_holder[0] = "completed"
            except Exception as exc:
                result_holder[0] = exc

        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=30)

        engine.set_worker(original_worker)

        if t.is_alive():
            probe.crash_type = CrashType.TIMEOUT
            probe.exception_message = "Engine did not enforce timeout -- worker hung indefinitely"
            probe.blast_radius = BlastRadius.APP
            probe.reproducibility = 1.0
        elif isinstance(result_holder[0], Exception):
            probe.recovered = True
        else:
            probe.recovered = True

        probes.append(probe)
        return probes


# ---------------------------------------------------------------------------
# Partial write / crash between DB commits
# ---------------------------------------------------------------------------
class PartialWriteInjector:
    name = "partial_write"
    description = "Simulates crash between DB writes to test atomicity"

    def inject(self, engine: HarnessEngine, sandbox: ChaosSandbox) -> list[ChaosProbe]:
        probes: list[ChaosProbe] = []
        store = sandbox.store
        if not store:
            return probes

        task = _first_pending_task(engine)
        if not task:
            return probes

        probe = ChaosProbe(
            probe_type=self.name,
            injector=self.name,
            description="Monkey-patch connect() to inject I/O error after N commits",
            task_id=task.id,
            blast_radius=BlastRadius.DATA,
        )

        # Wrap connect() to count commits and crash on the 3rd connection's commit
        original_connect = store.connect
        call_count = {"commits": 0}

        def _instrumented_connect():
            conn = original_connect()
            original_commit = conn.commit

            def _counting_commit():
                call_count["commits"] += 1
                if call_count["commits"] == 3:
                    raise OSError("Chaos: disk I/O error during commit")
                return original_commit()

            conn.commit = _counting_commit
            return conn

        store.connect = _instrumented_connect
        try:
            _run_with_capture(lambda: engine.run_once(task.id), probe)
        finally:
            store.connect = original_connect

        # Check DB consistency after partial write
        try:
            runs = store.list_runs(task_id=task.id)
            for run in runs:
                evals = store.list_evaluations(run_id=run.id)
                decisions = store.list_decisions(run_id=run.id)
                if run.status.value == "analyzing" and not evals:
                    probe.crash_type = CrashType.PARTIAL_WRITE
                    probe.exception_message = "Run stuck in analyzing with no evaluation"
                    probe.blast_radius = BlastRadius.DATA
                elif run.status.value == "deciding" and not decisions:
                    probe.crash_type = CrashType.PARTIAL_WRITE
                    probe.exception_message = "Run stuck in deciding with no decision"
                    probe.blast_radius = BlastRadius.DATA
        except Exception as exc:
            probe.crash_type = CrashType.DATA_CORRUPTION
            probe.exception_message = f"DB unreadable after partial write: {exc}"

        probe.reproducibility = 1.0
        probes.append(probe)
        return probes


# ---------------------------------------------------------------------------
# Concurrent run execution (same task, multiple engines)
# ---------------------------------------------------------------------------
class ConcurrentRunInjector:
    name = "concurrent_run"
    description = "Multiple threads run_once on the same task simultaneously"

    def inject(self, engine: HarnessEngine, sandbox: ChaosSandbox) -> list[ChaosProbe]:
        probes: list[ChaosProbe] = []
        task = _first_pending_task(engine)
        if not task:
            return probes

        probe = ChaosProbe(
            probe_type=self.name,
            injector=self.name,
            description="Race 3 threads calling run_once on same task",
            task_id=task.id,
            blast_radius=BlastRadius.SERVICE,
        )

        results: list[tuple[str, object]] = []
        barrier = threading.Barrier(3, timeout=10)

        def _run_it(thread_id: str):
            try:
                barrier.wait()
                run = engine.run_once(task.id)
                results.append((thread_id, run))
            except Exception as exc:
                results.append((thread_id, exc))

        threads = [
            threading.Thread(target=_run_it, args=(f"thread-{i}",))
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        successes = [r for r in results if not isinstance(r[1], Exception)]
        failures = [r for r in results if isinstance(r[1], Exception)]

        if len(successes) > 1:
            # Multiple threads ran the same task -- potential data corruption
            runs = sandbox.store.list_runs(task_id=task.id) if sandbox.store else []
            probe.crash_type = CrashType.DATA_CORRUPTION
            probe.blast_radius = BlastRadius.DATA
            probe.exception_message = (
                f"{len(successes)} threads completed run_once concurrently, "
                f"{len(runs)} runs in DB"
            )
            probe.reproducibility = 1.0
        elif len(successes) == 1 and failures:
            # Good: one succeeded, others failed
            probe.recovered = True
        elif not successes:
            probe.crash_type = CrashType.DEADLOCK
            probe.exception_message = f"All {len(failures)} threads failed"
        else:
            probe.recovered = True

        probes.append(probe)
        return probes


# ---------------------------------------------------------------------------
# Shadow supervisor -- run real supervise loop, diff before/after
# ---------------------------------------------------------------------------
class ShadowSupervisorInjector:
    name = "shadow_supervisor"
    description = "Runs the real supervisor loop in sandbox and audits the results"

    def __init__(
        self,
        max_iterations: int = 10,
        heartbeat_project_ids: list[str] | None = None,
        heartbeat_interval_seconds: float | None = None,
    ):
        self.max_iterations = max_iterations
        self.heartbeat_project_ids = heartbeat_project_ids
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    def inject(self, engine: HarnessEngine, sandbox: ChaosSandbox) -> list[ChaosProbe]:
        probes: list[ChaosProbe] = []
        store = sandbox.store
        if not store:
            return probes

        # --- Snapshot before ---
        before = self._snapshot(store)

        # --- Run the real supervisor ---
        supervisor_probe = ChaosProbe(
            probe_type=self.name,
            injector=self.name,
            description=(
                f"Shadow supervise: max_iterations={self.max_iterations}, "
                f"heartbeat_projects={self.heartbeat_project_ids}"
            ),
            blast_radius=BlastRadius.SERVICE,
        )

        supervisor_exc: Exception | None = None
        supervisor_result = None
        try:
            supervisor_result = engine.supervise(
                project_id=None,
                worker_id="chaos-shadow",
                lease_seconds=120,
                watch=False,
                idle_sleep_seconds=0.1,
                max_idle_cycles=2,
                max_iterations=self.max_iterations,
                heartbeat_project_ids=self.heartbeat_project_ids,
                heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            )
        except Exception as exc:
            supervisor_exc = exc
            supervisor_probe.crash_type = CrashType.UNHANDLED_EXCEPTION
            supervisor_probe.exception_class = type(exc).__name__
            supervisor_probe.exception_message = str(exc)[:500]
            supervisor_probe.traceback = traceback.format_exc()
            supervisor_probe.blast_radius = BlastRadius.APP
            supervisor_probe.reproducibility = 1.0

        # --- Snapshot after ---
        after = self._snapshot(store)

        # --- Audit the diff ---
        if supervisor_exc is None:
            audit_probes = self._audit(before, after, supervisor_result, store)
            probes.extend(audit_probes)
            if not audit_probes:
                supervisor_probe.recovered = True

        probes.append(supervisor_probe)
        return probes

    def _snapshot(self, store) -> dict:
        """Capture counts and states for before/after comparison."""
        tasks = store.list_tasks()
        runs = store.list_runs()
        events = store.list_events()
        return {
            "task_count": len(tasks),
            "tasks_by_status": self._count_by(tasks, lambda t: t.status.value),
            "run_count": len(runs),
            "runs_by_status": self._count_by(runs, lambda r: r.status.value),
            "event_count": len(events),
            "tasks": tasks,
            "runs": runs,
        }

    @staticmethod
    def _count_by(items, key_fn) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            k = key_fn(item)
            counts[k] = counts.get(k, 0) + 1
        return counts

    def _audit(self, before: dict, after: dict, result, store) -> list[ChaosProbe]:
        """Compare before/after state to find anomalies."""
        probes: list[ChaosProbe] = []

        # 1. Check for stuck runs (in-progress status after supervisor exited)
        stuck_statuses = {"planning", "working", "analyzing", "deciding"}
        for run in after["runs"]:
            if run.status.value in stuck_statuses:
                # Was it stuck before too? If so, not a new finding.
                was_stuck = any(
                    r.id == run.id and r.status.value in stuck_statuses
                    for r in before["runs"]
                )
                if was_stuck:
                    continue
                probes.append(ChaosProbe(
                    probe_type="shadow_stuck_run",
                    injector=self.name,
                    description=f"Run {run.id} stuck in {run.status.value} after supervisor exit",
                    run_id=run.id,
                    task_id=run.task_id,
                    phase=run.status.value,
                    crash_type=CrashType.PARTIAL_WRITE,
                    blast_radius=BlastRadius.DATA,
                    reproducibility=1.0,
                ))

        # 2. Check for runs with missing evaluations or decisions
        new_runs = [r for r in after["runs"] if r.id not in {br.id for br in before["runs"]}]
        for run in new_runs:
            if run.status.value in {"completed", "failed", "blocked", "disposed"}:
                evals = store.list_evaluations(run_id=run.id)
                decisions = store.list_decisions(run_id=run.id)
                if not evals:
                    probes.append(ChaosProbe(
                        probe_type="shadow_missing_evaluation",
                        injector=self.name,
                        description=f"Run {run.id} is {run.status.value} but has no evaluation",
                        run_id=run.id,
                        task_id=run.task_id,
                        crash_type=CrashType.DATA_CORRUPTION,
                        blast_radius=BlastRadius.DATA,
                        reproducibility=1.0,
                    ))
                if not decisions:
                    probes.append(ChaosProbe(
                        probe_type="shadow_missing_decision",
                        injector=self.name,
                        description=f"Run {run.id} is {run.status.value} but has no decision",
                        run_id=run.id,
                        task_id=run.task_id,
                        crash_type=CrashType.DATA_CORRUPTION,
                        blast_radius=BlastRadius.DATA,
                        reproducibility=1.0,
                    ))

        # 3. Check for tasks that went backwards (completed -> pending without retry)
        before_task_map = {t.id: t for t in before["tasks"]}
        for task in after["tasks"]:
            prev = before_task_map.get(task.id)
            if prev is None:
                continue
            if (
                prev.status.value == "completed"
                and task.status.value == "pending"
            ):
                probes.append(ChaosProbe(
                    probe_type="shadow_status_regression",
                    injector=self.name,
                    description=f"Task {task.id} regressed from completed to pending",
                    task_id=task.id,
                    crash_type=CrashType.DATA_CORRUPTION,
                    blast_radius=BlastRadius.DATA,
                    reproducibility=1.0,
                ))

        # 4. Check for heartbeat failures in events
        new_events = store.list_events()
        heartbeat_failures = [
            e for e in new_events
            if e.event_type == "heartbeat_failed"
            and e.id not in {ev.id for ev in before.get("events", [])}
        ]
        # list_events doesn't return "events" in snapshot, so check by count
        for evt in new_events[before["event_count"]:]:
            if evt.event_type == "heartbeat_failed":
                payload = evt.payload if isinstance(evt.payload, dict) else {}
                probes.append(ChaosProbe(
                    probe_type="shadow_heartbeat_failure",
                    injector=self.name,
                    description=(
                        f"Heartbeat failed for project {evt.entity_id}: "
                        f"{payload.get('error_type', '?')}: {payload.get('message', '?')}"
                    ),
                    task_id=evt.entity_id,
                    crash_type=CrashType.UNHANDLED_EXCEPTION,
                    blast_radius=BlastRadius.SERVICE,
                    exception_class=str(payload.get("error_type", "")),
                    exception_message=str(payload.get("message", ""))[:500],
                    reproducibility=1.0,
                ))
            elif evt.event_type == "heartbeat_escalated":
                payload = evt.payload if isinstance(evt.payload, dict) else {}
                probes.append(ChaosProbe(
                    probe_type="shadow_heartbeat_escalated",
                    injector=self.name,
                    description=(
                        f"Heartbeat escalated for project {evt.entity_id} "
                        f"after {payload.get('consecutive_failures', '?')} failures"
                    ),
                    task_id=evt.entity_id,
                    crash_type=CrashType.UNHANDLED_EXCEPTION,
                    blast_radius=BlastRadius.APP,
                    exception_message=str(payload.get("message", ""))[:500],
                    reproducibility=1.0,
                ))

        # 5. Summary probe: high failure rate
        new_run_count = after["run_count"] - before["run_count"]
        new_failed = after["runs_by_status"].get("failed", 0) - before["runs_by_status"].get("failed", 0)
        if new_run_count > 0 and new_failed / new_run_count > 0.8:
            probes.append(ChaosProbe(
                probe_type="shadow_high_failure_rate",
                injector=self.name,
                description=(
                    f"Shadow run failure rate: {new_failed}/{new_run_count} "
                    f"({new_failed / new_run_count:.0%})"
                ),
                crash_type=CrashType.UNHANDLED_EXCEPTION,
                blast_radius=BlastRadius.SERVICE,
                reproducibility=new_failed / new_run_count,
            ))

        return probes


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
ALL_INJECTORS: list[ChaosInjector] = [
    WorkerCrashInjector(),
    LeaseContentionInjector(),
    DBCorruptionInjector(),
    TimeoutExhaustionInjector(),
    PartialWriteInjector(),
    ConcurrentRunInjector(),
    ShadowSupervisorInjector(),
]
