from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class HarnessLogger:
    log_path: Path

    def __post_init__(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, **payload: Any) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            **payload,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def classify_error(exc: Exception) -> str:
    name = exc.__class__.__name__.lower()
    if "sqlite" in name:
        return "storage_error"
    if "subprocess" in name or "calledprocesserror" in name:
        return "integration_error"
    if isinstance(exc, ValueError):
        return "validation_error"
    return "unexpected_error"
