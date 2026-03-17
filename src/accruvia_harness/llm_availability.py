"""LLM availability gate — single chokepoint for checking whether any backend is reachable."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class LLMAvailabilityGate:
    """Tracks whether LLM backends are available and gates task execution.

    Probes are expensive (subprocess spawn + network round-trip), so results
    are cached.  On failure, the gate enters backoff (30s → 3600s max).  On
    success, the backoff resets and the result is cached for ``cache_ttl``
    seconds.
    """

    probe_fn: object  # Callable[[str, int], dict] — typically probe_llm_command
    commands: list[tuple[str, str]] = field(default_factory=list)  # [(name, command)]
    cache_ttl: float = 60.0
    backoff_seconds: float = 30.0
    max_backoff_seconds: float = 3600.0

    _available: bool | None = field(default=None, init=False, repr=False)
    _checked_at: float = field(default=0.0, init=False, repr=False)
    _unavailable_until: float = field(default=0.0, init=False, repr=False)
    _probe_results: dict[str, bool] = field(default_factory=dict, init=False, repr=False)

    def is_available(self) -> bool:
        """Return True if at least one LLM backend is reachable, using cached results."""
        now = time.monotonic()

        # Still in backoff window — return cached unavailable.
        if now < self._unavailable_until:
            return False

        # Cache still valid — return cached result.
        if self._available is not None and (now - self._checked_at) < self.cache_ttl:
            return self._available

        # Probe backends.
        self._checked_at = now
        self._probe_results = {}
        for name, command in self.commands:
            if not command:
                continue
            result = self.probe_fn(command, timeout_seconds=10)
            ok = bool(result.get("ok"))
            self._probe_results[name] = ok
            if ok:
                self._available = True
                self.backoff_seconds = 30.0  # Reset backoff on success.
                return True

        # All probes failed.
        self._available = False
        self._unavailable_until = now + self.backoff_seconds
        self.backoff_seconds = min(self.backoff_seconds * 2, self.max_backoff_seconds)
        return False

    def reset(self) -> None:
        """Force the next call to re-probe."""
        self._available = None
        self._checked_at = 0.0
        self._unavailable_until = 0.0
        self.backoff_seconds = 30.0

    @property
    def last_probe_results(self) -> dict[str, bool]:
        return dict(self._probe_results)

    @property
    def seconds_until_retry(self) -> float:
        remaining = self._unavailable_until - time.monotonic()
        return max(0.0, remaining)
