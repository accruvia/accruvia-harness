from __future__ import annotations

from pathlib import Path

from ..domain import Project, WorkspacePolicy
from ..project_adapters import ProjectWorkspace


class WorkspacePolicyViolation(RuntimeError):
    """Raised when a project adapter returns a workspace that violates project policy."""


class WorkspacePolicyEnforcer:
    def validate(self, project: Project, workspace: ProjectWorkspace) -> None:
        if project.workspace_policy == WorkspacePolicy.SHARED_ALLOWED:
            return
        if project.workspace_policy == WorkspacePolicy.ISOLATED_PREFERRED:
            return

        source_repo_root = workspace.source_repo_root
        if source_repo_root is None:
            return

        project_root = workspace.project_root.resolve()
        source_root = source_repo_root.resolve()

        if workspace.workspace_mode == "shared_repo":
            raise WorkspacePolicyViolation(
                f"Project {project.id} requires isolated workspaces, but adapter returned workspace_mode=shared_repo"
            )
        if project_root == source_root:
            raise WorkspacePolicyViolation(
                f"Project {project.id} requires isolated workspaces, but adapter returned the source repo root directly"
            )
        if workspace.project_root.is_symlink() and workspace.project_root.resolve() == source_root:
            raise WorkspacePolicyViolation(
                f"Project {project.id} requires isolated workspaces, but adapter symlinked the source repo"
            )

    @staticmethod
    def source_repo_changed(project_root: Path, source_repo_root: Path) -> bool:
        return project_root.resolve() == source_repo_root.resolve()
