from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..domain import Project, Run, Task


@dataclass(slots=True)
class ProjectWorkspace:
    project_root: Path
    metadata_files: list[Path] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    diagnostics: dict[str, object] = field(default_factory=dict)


class ProjectAdapter(Protocol):
    name: str

    def prepare_workspace(
        self,
        project: Project,
        task: Task,
        run: Run,
        run_dir: Path,
    ) -> ProjectWorkspace: ...

    def build_worker(
        self,
        project: Project,
        task: Task,
        run: Run,
        workspace: ProjectWorkspace,
        default_worker,
    ): ...
