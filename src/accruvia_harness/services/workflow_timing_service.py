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
