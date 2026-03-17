from __future__ import annotations

import os
import resource
from dataclasses import dataclass


LARGE_HEAP_BACKENDS = frozenset({"codex", "claude"})


def _total_memory_mb() -> int | None:
    meminfo = "/proc/meminfo"
    try:
        with open(meminfo, encoding="utf-8") as handle:
            for line in handle:
                if not line.startswith("MemTotal:"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    return max(int(parts[1]) // 1024, 1)
    except (OSError, ValueError):
        return None
    return None


def resolve_memory_limit_mb(configured_mb: int, *, backend_names: tuple[str, ...] = ()) -> int | None:
    requires_large_heap = any(name in LARGE_HEAP_BACKENDS for name in backend_names)

    # Node.js backends (codex, claude) need >4GB virtual address space just to
    # start.  RLIMIT_AS at any practical value breaks them, so skip it entirely.
    if requires_large_heap:
        return None

    base_limit_mb = max(int(configured_mb or 0), 1)
    total_memory_mb = _total_memory_mb()
    budget_mb = int(total_memory_mb * 0.8) if total_memory_mb is not None else None

    if budget_mb is None:
        return base_limit_mb
    return min(base_limit_mb, max(budget_mb, 1))


@dataclass(slots=True)
class ResourceLimitPolicy:
    memory_limit_mb: int | None
    cpu_time_limit_seconds: int

    def preexec_fn(self):
        memory_bytes = self.memory_limit_mb * 1024 * 1024 if self.memory_limit_mb is not None else None
        cpu_seconds = self.cpu_time_limit_seconds

        def _apply() -> None:
            if memory_bytes is not None:
                try:
                    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
                except (ValueError, OSError):
                    pass
                try:
                    resource.setrlimit(resource.RLIMIT_DATA, (memory_bytes, memory_bytes))
                except (ValueError, OSError):
                    pass
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            except (ValueError, OSError):
                pass
            os.setsid()

        return _apply
