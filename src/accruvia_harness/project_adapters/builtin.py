from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

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
        workspace_root = (run_dir / "workspace").resolve()
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


class CurrentRepoGitWorktreeAdapter:
    name = "current_repo_git_worktree"

    def prepare_workspace(
        self,
        project: Project,
        task: Task,
        run: Run,
        run_dir: Path,
    ) -> ProjectWorkspace:
        source_repo_root = self._resolve_source_repo_root()
        workspace_root = (run_dir / "workspace").resolve()
        base_ref = self._resolve_base_ref(source_repo_root, project.base_branch)
        branch_name = f"harness-{task.id[-6:]}-{run.id[-6:]}"
        subprocess.run(
            [
                "git",
                "-C",
                str(source_repo_root),
                "worktree",
                "add",
                "-b",
                branch_name,
                str(workspace_root),
                base_ref,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        manifest_path = run_dir / "workspace_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "project_id": project.id,
                    "project_name": project.name,
                    "project_adapter": self.name,
                    "task_id": task.id,
                    "task_title": task.title,
                    "run_id": run.id,
                    "validation_profile": task.validation_profile,
                    "source_repo_root": str(source_repo_root),
                    "workspace_root": str(workspace_root),
                    "branch_name": branch_name,
                    "base_ref": base_ref,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return ProjectWorkspace(
            project_root=workspace_root,
            workspace_mode="git_worktree",
            source_repo_root=source_repo_root,
            branch_name=branch_name,
            metadata_files=[manifest_path],
            environment={
                "ACCRUVIA_PROJECT_WORKSPACE": str(workspace_root),
                "ACCRUVIA_SOURCE_REPO_ROOT": str(source_repo_root),
                "ACCRUVIA_PROJECT_MANIFEST_PATH": str(manifest_path),
                "ACCRUVIA_WORKTREE_BRANCH": branch_name,
            },
            diagnostics={
                "project_adapter": self.name,
                "source_repo_root": str(source_repo_root),
                "base_ref": base_ref,
                "branch_name": branch_name,
            },
        )

    @staticmethod
    def _resolve_source_repo_root() -> Path:
        configured = os.environ.get("ACCRUVIA_SOURCE_REPO_ROOT")
        if configured:
            return Path(configured).resolve()
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
        return Path(completed.stdout.strip()).resolve()

    @staticmethod
    def _resolve_base_ref(source_repo_root: Path, preferred: str | None) -> str:
        candidates = [preferred, f"origin/{preferred}" if preferred else None, "HEAD"]
        for candidate in candidates:
            if not candidate:
                continue
            completed = subprocess.run(
                ["git", "-C", str(source_repo_root), "rev-parse", "--verify", candidate],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode == 0:
                return candidate
        return "HEAD"


def builtin_project_adapters() -> list[object]:
    return [GenericProjectAdapter(), CurrentRepoGitWorktreeAdapter()]
