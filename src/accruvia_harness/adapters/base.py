from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain import Task


@dataclass(slots=True)
class AdapterEvidence:
    passed: bool
    report: dict[str, object]
    diagnostics: dict[str, object]


class WorkloadAdapter(Protocol):
    profile: str

    def build_evidence(self, task: Task, run_dir: Path) -> AdapterEvidence: ...
