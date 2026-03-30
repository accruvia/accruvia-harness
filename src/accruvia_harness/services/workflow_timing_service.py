from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def _parse_timestamp(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


@dataclass(slots=True)
class WorkflowTimingService:
    @staticmethod
    def duration_ms(
        started_at: datetime | str | None,
        *,
        completed_at: datetime | str | None = None,
        failed_at: datetime | str | None = None,
        last_activity_at: datetime | str | None = None,
    ) -> int:
        start = _parse_timestamp(started_at)
        if start is None:
            return 0
        end = _parse_timestamp(completed_at) or _parse_timestamp(failed_at) or _parse_timestamp(last_activity_at)
        if end is None:
            return 0
        return max(0, int((end - start).total_seconds() * 1000))

    @staticmethod
    def sequential_phase_rows(
        started_at: datetime | str | None,
        phases: list[tuple[str, datetime | str | None]],
        *,
        completed_at: datetime | str | None = None,
        failed_at: datetime | str | None = None,
        last_activity_at: datetime | str | None = None,
    ) -> list[dict[str, object]]:
        start = _parse_timestamp(started_at)
        if start is None or not phases:
            return []
        phase_points: list[tuple[str, datetime]] = []
        for name, timestamp in phases:
            parsed = _parse_timestamp(timestamp)
            if parsed is None:
                continue
            phase_points.append((str(name), parsed))
        if not phase_points:
            return []
        final_end = (
            _parse_timestamp(completed_at)
            or _parse_timestamp(failed_at)
            or _parse_timestamp(last_activity_at)
            or phase_points[-1][1]
        )
        rows: list[dict[str, object]] = []
        for index, (phase_name, phase_started_at) in enumerate(phase_points):
            row_started_at = start if index == 0 else phase_started_at
            next_phase = phase_points[index + 1][1] if index + 1 < len(phase_points) else final_end
            row_ended_at = next_phase or phase_started_at
            rows.append(
                {
                    "phase": phase_name,
                    "started_at": row_started_at.isoformat(),
                    "ended_at": row_ended_at.isoformat(),
                    "duration_ms": max(0, int((row_ended_at - row_started_at).total_seconds() * 1000)),
                }
            )
        return rows
