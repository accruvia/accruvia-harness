"""Cost aggregation and daily budget enforcement for LLM runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

try:
    from filelock import FileLock
except ImportError:  # pragma: no cover

    class FileLock:  # type: ignore[no-redef]
        """No-op fallback when filelock is not installed."""

        def __init__(self, lock_file: str | Path) -> None:
            pass

        def __enter__(self) -> "FileLock":
            return self

        def __exit__(self, *args: object) -> None:
            pass


class CostTracker:
    """Aggregates LLM run costs per project per day using a JSON ledger.

    The ledger is a single JSON file keyed by ``{project_id}:{YYYY-MM-DD}``
    where each value is the cumulative cost_usd for that project-day.
    """

    def __init__(self, ledger_path: Path | None = None) -> None:
        if ledger_path is None:
            ledger_path = Path(".accruvia-harness") / "cost_ledger.json"
        self._ledger_path = ledger_path
        self._lock_path = ledger_path.with_suffix(".lock")

    def record_run_cost(self, project_id: str, run_id: str, run_dir: str | Path) -> float:
        """Read ``llm_metadata.json`` from *run_dir* and add its cost to the daily ledger.

        Returns the ``cost_usd`` value found, or ``0.0`` if the file is
        missing or malformed.
        """
        metadata_path = Path(run_dir) / "llm_metadata.json"
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            cost_usd = float(data.get("cost_usd") or 0.0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            cost_usd = 0.0

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{project_id}:{today}"

        with FileLock(str(self._lock_path)):
            ledger = self._load_ledger()
            ledger[key] = ledger.get(key, 0.0) + cost_usd
            self._save_ledger(ledger)

        return cost_usd

    def daily_cost(self, project_id: str, date: str | None = None) -> float:
        """Return total ``cost_usd`` for *project_id* on *date* (``YYYY-MM-DD``, default today)."""
        if date is None:
            date = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{project_id}:{date}"
        return self._load_ledger().get(key, 0.0)

    def check_budget(
        self,
        project_id: str,
        daily_limit_usd: float = 20.0,
    ) -> tuple[bool, float]:
        """Check whether *project_id* is within its daily budget.

        Returns ``(within_budget, remaining_usd)``.
        """
        spent = self.daily_cost(project_id)
        remaining = daily_limit_usd - spent
        return (remaining >= 0.0, remaining)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_ledger(self) -> dict[str, float]:
        if not self._ledger_path.exists():
            return {}
        try:
            data = json.loads(self._ledger_path.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in data.items()}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_ledger(self, ledger: dict[str, float]) -> None:
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._ledger_path.write_text(
            json.dumps(ledger, indent=2, sort_keys=True),
            encoding="utf-8",
        )
