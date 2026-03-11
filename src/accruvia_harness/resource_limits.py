from __future__ import annotations

import os
import resource
from dataclasses import dataclass


@dataclass(slots=True)
class ResourceLimitPolicy:
    memory_limit_mb: int
    cpu_time_limit_seconds: int

    def preexec_fn(self):
        memory_bytes = self.memory_limit_mb * 1024 * 1024
        cpu_seconds = self.cpu_time_limit_seconds

        def _apply() -> None:
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
