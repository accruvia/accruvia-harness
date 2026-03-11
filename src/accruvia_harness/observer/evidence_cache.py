"""Rolling evidence cache with snapshot diffing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(slots=True)
class Snapshot:
    timestamp: datetime
    data: dict
    source: str


class EvidenceCache:
    """Maintains rolling snapshots from harness queries for temporal reasoning."""

    def __init__(self, max_snapshots: int = 20) -> None:
        self.max_snapshots = max_snapshots
        self._snapshots: list[Snapshot] = []

    def record(self, source: str, data: dict) -> Snapshot:
        snapshot = Snapshot(
            timestamp=datetime.now(UTC),
            data=data,
            source=source,
        )
        self._snapshots.append(snapshot)
        if len(self._snapshots) > self.max_snapshots:
            self._snapshots = self._snapshots[-self.max_snapshots:]
        return snapshot

    def latest(self, source: str | None = None) -> Snapshot | None:
        for snapshot in reversed(self._snapshots):
            if source is None or snapshot.source == source:
                return snapshot
        return None

    def history(self, source: str | None = None, limit: int = 5) -> list[Snapshot]:
        matches = [s for s in self._snapshots if source is None or s.source == source]
        return matches[-limit:]

    def diff_latest(self, source: str) -> dict | None:
        """Compare the two most recent snapshots of a source, return key changes."""
        matching = [s for s in self._snapshots if s.source == source]
        if len(matching) < 2:
            return None
        prev, curr = matching[-2], matching[-1]
        return self._diff_dicts(prev.data, curr.data)

    def summary_text(self) -> str:
        """Produce a human-readable summary of cached evidence."""
        if not self._snapshots:
            return "No evidence cached yet."
        sources = {}
        for s in self._snapshots:
            sources[s.source] = s
        lines = []
        for source, snap in sources.items():
            age = (datetime.now(UTC) - snap.timestamp).total_seconds()
            if age < 60:
                age_str = f"{int(age)}s ago"
            elif age < 3600:
                age_str = f"{int(age / 60)}m ago"
            else:
                age_str = f"{int(age / 3600)}h ago"
            lines.append(f"  {source}: last updated {age_str}")
        return "Cached evidence:\n" + "\n".join(lines)

    @staticmethod
    def _diff_dicts(old: dict, new: dict) -> dict:
        changes: dict[str, object] = {}
        all_keys = set(old) | set(new)
        for key in all_keys:
            old_val = old.get(key)
            new_val = new.get(key)
            if old_val != new_val:
                if isinstance(old_val, dict) and isinstance(new_val, dict):
                    nested = EvidenceCache._diff_dicts(old_val, new_val)
                    if nested:
                        changes[key] = nested
                else:
                    changes[key] = {"old": old_val, "new": new_val}
        return changes
