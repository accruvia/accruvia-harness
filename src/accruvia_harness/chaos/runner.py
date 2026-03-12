"""Chaos monkey runner -- orchestrates injectors and feeds findings back."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from accruvia_harness.chaos.domain import ChaosProbe, ChaosRound, Severity
from accruvia_harness.chaos.injectors import ALL_INJECTORS, ChaosInjector
from accruvia_harness.chaos.sandbox import ChaosSandbox, chaos_sandbox
from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import Event, new_id
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.store import SQLiteHarnessStore

logger = logging.getLogger(__name__)


class ChaosRunner:
    """Runs chaos injectors in an isolated sandbox, feeds results to heartbeat."""

    def __init__(
        self,
        config: HarnessConfig,
        injectors: list[ChaosInjector] | None = None,
        memory_limit_mb: int = 2048,
        cpu_limit_seconds: int = 300,
        feed_to_project_id: str = "",
    ):
        self.config = config
        self.injectors = injectors or list(ALL_INJECTORS)
        self.memory_limit_mb = memory_limit_mb
        self.cpu_limit_seconds = cpu_limit_seconds
        self.feed_to_project_id = feed_to_project_id

    def run(self) -> ChaosRound:
        """Execute all injectors in sandbox, return round results."""
        chaos_round = ChaosRound()
        source_db = Path(self.config.db_path)

        if not source_db.exists():
            logger.error("Source DB does not exist: %s", source_db)
            return chaos_round

        source_repo = Path(self.config.workspace_root).parent
        if not (source_repo / ".git").exists():
            source_repo = None

        with chaos_sandbox(
            source_db=source_db,
            source_repo=source_repo,
            memory_limit_mb=self.memory_limit_mb,
            cpu_limit_seconds=self.cpu_limit_seconds,
        ) as sandbox:
            engine = self._build_sandbox_engine(sandbox)

            for injector in self.injectors:
                logger.info("Running injector: %s", injector.name)
                chaos_round.injectors_run += 1
                try:
                    probes = injector.inject(engine, sandbox)
                    for probe in probes:
                        chaos_round.probes.append(probe)
                        if probe.crash_type is not None:
                            chaos_round.errors_found += 1
                            logger.warning(
                                "Finding: %s [%s] score=%.1f",
                                probe.probe_type,
                                probe.severity().value,
                                probe.severity_score(),
                            )
                except Exception as exc:
                    probe = ChaosProbe(
                        probe_type=f"{injector.name}_meta_crash",
                        injector=injector.name,
                        description=f"Injector crashed: {exc}",
                        exception_class=type(exc).__name__,
                        exception_message=str(exc)[:500],
                    )
                    chaos_round.probes.append(probe)
                    chaos_round.errors_found += 1
                    logger.error("Injector %s crashed: %s", injector.name, exc)

        chaos_round.finished_at = datetime.now(timezone.utc)
        return chaos_round

    def run_and_feed(self, production_store: SQLiteHarnessStore) -> ChaosRound:
        """Run chaos, then feed HIGH+ findings into production as events."""
        chaos_round = self.run()

        for probe in chaos_round.probes:
            if probe.severity() not in (Severity.CRITICAL, Severity.HIGH):
                continue
            if not self.feed_to_project_id:
                continue

            task_spec = probe.to_heartbeat_task()
            try:
                production_store.create_event(
                    Event(
                        id=new_id("event"),
                        entity_type="project",
                        entity_id=self.feed_to_project_id,
                        event_type="chaos_finding",
                        payload={
                            "probe_id": probe.id,
                            "probe_type": probe.probe_type,
                            "severity": probe.severity().value,
                            "score": probe.severity_score(),
                            "proposed_task": task_spec,
                        },
                    )
                )
            except Exception as exc:
                logger.error("Failed to feed probe %s: %s", probe.id, exc)

        logger.info(
            "Chaos round %s: %d injectors, %d probes, %d errors",
            chaos_round.id,
            chaos_round.injectors_run,
            len(chaos_round.probes),
            chaos_round.errors_found,
        )
        return chaos_round

    def _build_sandbox_engine(self, sandbox: ChaosSandbox) -> HarnessEngine:
        workspace = sandbox.worktree_path or sandbox.sandbox_root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return HarnessEngine(
            store=sandbox.store,
            workspace_root=workspace,
        )


def write_chaos_report(chaos_round: ChaosRound, output_path: Path) -> None:
    """Write chaos round results as JSON for external consumption."""
    report = chaos_round.summary()
    report["probes"] = []
    for p in chaos_round.probes:
        report["probes"].append({
            "id": p.id,
            "type": p.probe_type,
            "crash_type": p.crash_type.value if p.crash_type else None,
            "severity": p.severity().value,
            "score": p.severity_score(),
            "blast_radius": p.blast_radius.value,
            "recovered": p.recovered,
            "reproducibility": p.reproducibility,
            "user_controllable": p.user_controllable,
            "description": p.description,
            "exception": f"{p.exception_class}: {p.exception_message}" if p.exception_class else None,
            "phase": p.phase,
            "task_id": p.task_id,
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, default=str))
    logger.info("Chaos report written to %s", output_path)
