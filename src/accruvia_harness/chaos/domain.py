"""Domain types for chaos testing."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


class CrashType(enum.Enum):
    SEGFAULT = "segfault"
    OOM = "oom"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    DEADLOCK = "deadlock"
    DATA_CORRUPTION = "data_corruption"
    TIMEOUT = "timeout"
    VALIDATION_BYPASS = "validation_bypass"
    STALE_LOCK = "stale_lock"
    PARTIAL_WRITE = "partial_write"


class BlastRadius(enum.Enum):
    WORKER = "worker"       # single worker/task affected
    SERVICE = "service"     # service-level impact
    APP = "app"             # whole engine crash
    DATA = "data"           # persistent state corrupted


class Severity(enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class ChaosProbe:
    """A single chaos injection and its observed result."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    probe_type: str = ""
    injector: str = ""
    description: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    crash_type: CrashType | None = None
    exception_class: str = ""
    exception_message: str = ""
    traceback: str = ""

    blast_radius: BlastRadius = BlastRadius.WORKER
    recovered: bool = False
    recovery_seconds: float = 0.0
    user_controllable: bool = False
    reproducibility: float = 0.0

    task_id: str = ""
    run_id: str = ""
    phase: str = ""

    def severity_score(self) -> float:
        base = {
            CrashType.SEGFAULT: 10,
            CrashType.OOM: 10,
            CrashType.DATA_CORRUPTION: 10,
            CrashType.DEADLOCK: 9,
            CrashType.UNHANDLED_EXCEPTION: 7,
            CrashType.VALIDATION_BYPASS: 8,
            CrashType.STALE_LOCK: 5,
            CrashType.PARTIAL_WRITE: 8,
            CrashType.TIMEOUT: 3,
            None: 1,
        }.get(self.crash_type, 1)

        radius_mult = {
            BlastRadius.WORKER: 1.0,
            BlastRadius.SERVICE: 1.5,
            BlastRadius.APP: 2.0,
            BlastRadius.DATA: 3.0,
        }[self.blast_radius]

        recovery_div = 2.0 if self.recovered else 1.0
        control_mult = 1.5 if self.user_controllable else 1.0
        repro = max(self.reproducibility, 0.1)

        return (base * radius_mult * control_mult * repro) / recovery_div

    def severity(self) -> Severity:
        score = self.severity_score()
        if score >= 12:
            return Severity.CRITICAL
        if score >= 7:
            return Severity.HIGH
        if score >= 3:
            return Severity.MEDIUM
        return Severity.LOW

    def to_heartbeat_task(self) -> dict:
        """Format as a proposed task for heartbeat ingestion."""
        sev = self.severity()
        priority_map = {
            Severity.CRITICAL: "P0",
            Severity.HIGH: "P1",
            Severity.MEDIUM: "P2",
            Severity.LOW: "P3",
        }
        return {
            "title": f"[chaos] {self.probe_type}: {self.crash_type.value if self.crash_type else 'unknown'}",
            "objective": (
                f"Chaos probe {self.id} found: {self.description}\n"
                f"Exception: {self.exception_class}: {self.exception_message}\n"
                f"Phase: {self.phase}, Blast radius: {self.blast_radius.value}, "
                f"Recovered: {self.recovered}, Reproducibility: {self.reproducibility:.0%}\n"
                f"Score: {self.severity_score():.1f} -> {sev.value}"
            ),
            "priority": priority_map[sev],
            "strategy": "fix",
            "validation_profile": "default",
            "max_attempts": 3,
        }


@dataclass
class ChaosRound:
    """Results from one chaos testing round."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    probes: list[ChaosProbe] = field(default_factory=list)
    injectors_run: int = 0
    errors_found: int = 0

    def critical_probes(self) -> list[ChaosProbe]:
        return [p for p in self.probes if p.severity() == Severity.CRITICAL]

    def summary(self) -> dict:
        by_severity: dict[str, int] = {}
        for p in self.probes:
            s = p.severity().value
            by_severity[s] = by_severity.get(s, 0) + 1
        return {
            "round_id": self.id,
            "injectors_run": self.injectors_run,
            "probes": len(self.probes),
            "errors_found": self.errors_found,
            "by_severity": by_severity,
            "critical_count": len(self.critical_probes()),
        }
