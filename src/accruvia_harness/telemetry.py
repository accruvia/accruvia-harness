from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TelemetrySink:
    root: Path
    metrics_path: Path = field(init=False)
    spans_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.metrics_path = self.root / "metrics.jsonl"
        self.spans_path = self.root / "spans.jsonl"

    def metric(self, name: str, value: float, **attributes: Any) -> None:
        self._append(
            self.metrics_path,
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "type": "metric",
                "name": name,
                "value": value,
                "attributes": attributes,
            },
        )

    def span(self, name: str, duration_ms: float | None = None, **attributes: Any) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "span",
            "name": name,
            "attributes": attributes,
        }
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        self._append(self.spans_path, payload)

    def timed(self, name: str, **attributes: Any):
        return _TimedSpan(self, name, attributes)

    def summary(self) -> dict[str, object]:
        metrics = self._load(self.metrics_path)
        spans = self._load(self.spans_path)
        by_name: dict[str, float] = {}
        for item in metrics:
            by_name[item["name"]] = by_name.get(item["name"], 0.0) + float(item["value"])
        span_counts: dict[str, int] = {}
        span_durations: dict[str, list[float]] = {}
        for item in spans:
            name = str(item["name"])
            span_counts[name] = span_counts.get(name, 0) + 1
            if "duration_ms" in item:
                span_durations.setdefault(name, []).append(float(item["duration_ms"]))
        return {
            "metrics_path": str(self.metrics_path),
            "spans_path": str(self.spans_path),
            "metric_totals": by_name,
            "span_counts": span_counts,
            "span_average_ms": {
                name: (sum(values) / len(values)) for name, values in span_durations.items() if values
            },
        }

    def _append(self, path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _load(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            records.append(json.loads(line))
        return records


class _TimedSpan:
    def __init__(self, sink: TelemetrySink, name: str, attributes: dict[str, Any]) -> None:
        self.sink = sink
        self.name = name
        self.attributes = attributes
        self.started = 0.0

    def __enter__(self) -> "_TimedSpan":
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        duration_ms = (time.perf_counter() - self.started) * 1000
        payload = dict(self.attributes)
        if exc_type is not None:
            payload["error"] = exc_type.__name__
        self.sink.span(self.name, duration_ms=duration_ms, **payload)
