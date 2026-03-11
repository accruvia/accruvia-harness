from __future__ import annotations

import json
from pathlib import Path

from ..domain import Project, Run, Task
from .base import ProjectWorkspace


class GenericProjectAdapter:
    name = "generic"

    def prepare_workspace(
        self,
        project: Project,
        task: Task,
        run: Run,
        run_dir: Path,
    ) -> ProjectWorkspace:
        workspace_root = run_dir / "workspace"
        workspace_root.mkdir(parents=True, exist_ok=True)
        manifest_path = workspace_root / "workspace_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "project_id": project.id,
                    "project_name": project.name,
                    "project_adapter": project.adapter_name,
                    "task_id": task.id,
                    "task_title": task.title,
                    "run_id": run.id,
                    "validation_profile": task.validation_profile,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return ProjectWorkspace(
            project_root=workspace_root,
            workspace_mode="ephemeral_dir",
            metadata_files=[manifest_path],
            environment={
                "ACCRUVIA_PROJECT_WORKSPACE": str(workspace_root),
                "ACCRUVIA_PROJECT_MANIFEST_PATH": str(manifest_path),
            },
            diagnostics={"project_adapter": self.name},
        )


def builtin_project_adapters() -> list[GenericProjectAdapter]:
    return [GenericProjectAdapter()]
