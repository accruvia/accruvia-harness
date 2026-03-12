"""Heartbeat integration -- materializes chaos findings as prioritized tasks.

Flow:
    ChaosRunner (sandbox) -> chaos_finding events -> drain_chaos_findings() -> tasks
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from accruvia_harness.domain import Event, TaskStatus, new_id, serialize_dataclass
from accruvia_harness.store import SQLiteHarnessStore

logger = logging.getLogger(__name__)


@dataclass
class ChaosDrainResult:
    created_tasks: list[dict]
    skipped_duplicates: int
    total_findings: int


def drain_chaos_findings(
    store: SQLiteHarnessStore,
    project_id: str,
    task_service,
    min_severity: str = "high",
) -> ChaosDrainResult:
    """Read chaos_finding events and materialize as tasks."""
    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    min_rank = severity_rank.get(min_severity, 3)

    # list_events only filters by entity_type and entity_id
    all_events = store.list_events(
        entity_type="project",
        entity_id=project_id,
    )
    chaos_events = [e for e in all_events if e.event_type == "chaos_finding"]

    existing_tasks = store.list_tasks(project_id)
    existing_titles = {
        t.title
        for t in existing_tasks
        if t.status in {TaskStatus.PENDING, TaskStatus.ACTIVE}
    }

    created: list[dict] = []
    skipped = 0

    for event in chaos_events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        sev = payload.get("severity", "low")
        if severity_rank.get(sev, 0) < min_rank:
            continue

        proposed = payload.get("proposed_task", {})
        title = proposed.get("title", "")
        if not title:
            continue

        if title in existing_titles:
            skipped += 1
            continue

        task = task_service.create_task_with_policy(
            project_id=project_id,
            title=title,
            objective=proposed.get("objective", ""),
            priority=_parse_chaos_priority(proposed.get("priority", "P2")),
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy=proposed.get("strategy", "fix"),
            validation_profile=proposed.get("validation_profile", "default"),
            max_attempts=proposed.get("max_attempts", 3),
            max_branches=1,
            required_artifacts=["plan", "report"],
        )

        existing_titles.add(title)

        store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="chaos_task_created",
                payload={
                    "project_id": project_id,
                    "source": "chaos_monkey",
                    "probe_id": payload.get("probe_id", ""),
                    "probe_type": payload.get("probe_type", ""),
                    "severity": sev,
                    "score": payload.get("score", 0),
                },
            )
        )

        created.append(serialize_dataclass(task))
        logger.info(
            "Created chaos task %s: %s (severity=%s, score=%.1f)",
            task.id, title, sev, payload.get("score", 0),
        )

    return ChaosDrainResult(
        created_tasks=created,
        skipped_duplicates=skipped,
        total_findings=len(chaos_events),
    )


def _parse_chaos_priority(value: str | int) -> int:
    if isinstance(value, int):
        return value
    return {
        "P0": 1000,
        "P1": 700,
        "P2": 400,
        "P3": 200,
    }.get(str(value).upper(), 400)
