from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass(slots=True)
class ExecutionTimeoutPolicy:
    telemetry: object | None
    alpha: float = 0.5
    min_seconds: int = 30
    max_seconds: int = 1800
    multiplier: float = 2.5
    aggressive_failure_categories: tuple[str, ...] = (
        "executor_timeout",
        "validation_timeout",
        "compile_timeout",
        "git_timeout",
        "task_run_timeout",
    )

    def _aggressive_multiplier(self, validation_profile: str, worker_backend: str) -> float:
        if self.telemetry is None or not hasattr(self.telemetry, "load_warnings"):
            return self.multiplier
        warnings = self.telemetry.load_warnings()
        timeout_hits = 0
        for item in reversed(warnings[-50:]):
            category = str(item.get("category") or "")
            if category not in self.aggressive_failure_categories:
                continue
            attrs = item.get("attributes", {}) or {}
            warning_profile = str(attrs.get("validation_profile") or "")
            warning_backend = str(attrs.get("worker_backend") or attrs.get("backend_name") or "")
            if warning_profile and warning_profile != validation_profile:
                continue
            if warning_backend and warning_backend != worker_backend:
                continue
            timeout_hits += 1
        if timeout_hits >= 3:
            return max(1.25, self.multiplier * 0.6)
        if timeout_hits >= 1:
            return max(1.5, self.multiplier * 0.8)
        return self.multiplier

    def timeout_seconds(self, validation_profile: str, worker_backend: str) -> int:
        if self.telemetry is None:
            return self.min_seconds
        spans = self.telemetry.load_spans()
        durations = [
            float(item["duration_ms"]) / 1000.0
            for item in spans
            if item.get("name") == "work"
            and item.get("attributes", {}).get("validation_profile") == validation_profile
            and "duration_ms" in item
            and not item.get("attributes", {}).get("error")
        ]
        if not durations:
            durations = [
                float(item["duration_ms"]) / 1000.0
                for item in spans
                if item.get("name") == "work"
                and item.get("attributes", {}).get("worker_backend") == worker_backend
                and "duration_ms" in item
                and not item.get("attributes", {}).get("error")
            ]
        if not durations:
            return self.min_seconds
        ema = durations[0]
        for sample in durations[1:]:
            ema = self.alpha * sample + (1.0 - self.alpha) * ema
        multiplier = self._aggressive_multiplier(validation_profile, worker_backend)
        timeout = int(max(self.min_seconds, min(self.max_seconds, ema * multiplier)))
        return timeout

    def describe(self, validation_profile: str, worker_backend: str) -> dict[str, Any]:
        timeout_seconds = self.timeout_seconds(validation_profile, worker_backend)
        return {
            "validation_profile": validation_profile,
            "worker_backend": worker_backend,
            "timeout_seconds": timeout_seconds,
            "alpha": self.alpha,
            "multiplier": self._aggressive_multiplier(validation_profile, worker_backend),
            "base_multiplier": self.multiplier,
            "min_seconds": self.min_seconds,
            "max_seconds": self.max_seconds,
        }
