from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..domain import PromotionMode, Project, RepoProvider, Task
from ..github import GitHubCLI
from ..gitlab import GitLabCLI


@dataclass(slots=True)
class PromotionApplyResult:
    branch_name: str
    commit_sha: str
    pushed_ref: str
    pr_url: str | None = None


class RepositoryPromotionService:
    def __init__(self, github: GitHubCLI | None = None, gitlab: GitLabCLI | None = None) -> None:
        self.github = github or GitHubCLI()
        self.gitlab = gitlab or GitLabCLI()

    def apply(
        self,
        project: Project,
        task: Task,
        workspace_root: Path,
        *,
        target_branch: str | None = None,
        open_review: bool | None = None,
    ) -> PromotionApplyResult:
        branch_name = self._branch_name(workspace_root)
        if not branch_name:
            raise RuntimeError("Promotion apply-back requires an isolated git branch in the prepared workspace")
        if not self._has_changes(workspace_root):
            raise RuntimeError("Promotion apply-back requires modified files in the prepared workspace")

        self._git(workspace_root, "add", "-A")
        self._git(workspace_root, "commit", "-m", self._commit_message(task))
        commit_sha = self._git_output(workspace_root, "rev-parse", "HEAD").strip()

        if project.promotion_mode == PromotionMode.DIRECT_MAIN:
            pushed_ref = f"{commit_sha}:{project.base_branch}"
            self._git(workspace_root, "push", "origin", f"HEAD:{project.base_branch}")
            return PromotionApplyResult(branch_name=branch_name, commit_sha=commit_sha, pushed_ref=pushed_ref)

        push_branch = target_branch or branch_name
        self._git(workspace_root, "push", "-u", "origin", f"HEAD:{push_branch}")
        pr_url = None
        should_open_review = (
            open_review if open_review is not None else project.promotion_mode == PromotionMode.BRANCH_AND_PR
        )
        if should_open_review:
            pr_url = self._open_review(project, task, push_branch)
        return PromotionApplyResult(
            branch_name=push_branch,
            commit_sha=commit_sha,
            pushed_ref=push_branch,
            pr_url=pr_url,
        )

    def _open_review(self, project: Project, task: Task, branch_name: str) -> str | None:
        if not project.repo_name or not project.repo_provider:
            raise RuntimeError("Promotion mode branch_and_pr requires project repo_name and repo_provider")
        title = task.title
        body = f"Automated promotion for task {task.id}\n\nObjective:\n{task.objective}"
        if project.repo_provider == RepoProvider.GITHUB:
            return self.github.create_pull_request(
                project.repo_name,
                title=title,
                body=body,
                head=branch_name,
                base=project.base_branch,
            )
        if project.repo_provider == RepoProvider.GITLAB:
            return self.gitlab.create_merge_request(
                project.repo_name,
                title=title,
                body=body,
                source_branch=branch_name,
                target_branch=project.base_branch,
            )
        raise RuntimeError(f"Unsupported repo provider for review creation: {project.repo_provider}")

    @staticmethod
    def _commit_message(task: Task) -> str:
        return f"{task.title} ({task.id})"

    @staticmethod
    def _branch_name(workspace_root: Path) -> str:
        return RepositoryPromotionService._git_output(workspace_root, "rev-parse", "--abbrev-ref", "HEAD").strip()

    @staticmethod
    def _has_changes(workspace_root: Path) -> bool:
        return bool(RepositoryPromotionService._git_output(workspace_root, "status", "--porcelain").strip())

    @staticmethod
    def _git_output(workspace_root: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout

    @staticmethod
    def _git(workspace_root: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
