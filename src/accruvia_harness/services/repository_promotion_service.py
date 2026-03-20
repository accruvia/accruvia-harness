from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
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
    cleanup_performed: bool = False
    verified_remote_sha: str | None = None


class RepositoryPromotionService:
    def __init__(
        self,
        github: GitHubCLI | None = None,
        gitlab: GitLabCLI | None = None,
        *,
        pre_push_commands: tuple[tuple[str, ...], ...] | None = None,
    ) -> None:
        self.github = github or GitHubCLI()
        self.gitlab = gitlab or GitLabCLI()
        self.pre_push_commands = pre_push_commands if pre_push_commands is not None else (
            ("make", "verify-test-import-safety"),
            ("make", "test-fast"),
        )

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
        self._run_pre_push_checks(workspace_root)

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

    def apply_objective(
        self,
        project: Project,
        *,
        objective_id: str,
        objective_title: str,
        source_repo_root: Path,
        source_working_root: Path,
        objective_paths: list[str],
        staging_root: Path,
    ) -> PromotionApplyResult:
        cleaned_paths = self._normalize_objective_paths(objective_paths)
        if not cleaned_paths:
            raise RuntimeError("Objective promotion requires at least one tracked objective file path")

        source_repo_root = source_repo_root.resolve()
        source_working_root = source_working_root.resolve()
        staging_root.mkdir(parents=True, exist_ok=True)
        branch_name = f"objective-{objective_id[-6:]}-{next(tempfile._get_candidate_names())[:6]}"
        worktree_root = Path(tempfile.mkdtemp(prefix=f"{branch_name}-", dir=staging_root)).resolve()
        base_ref = self._refresh_base_ref(source_repo_root, project.base_branch)
        worktree_added = False
        push_result: PromotionApplyResult | None = None
        try:
            self._git(source_repo_root, "worktree", "add", "-b", branch_name, str(worktree_root), base_ref)
            worktree_added = True
            self._sync_objective_paths(
                source_working_root=source_working_root,
                worktree_root=worktree_root,
                objective_paths=cleaned_paths,
            )
            if not self._has_changes(worktree_root):
                raise RuntimeError("Objective promotion found no staged changes after syncing objective files")
            self._git(worktree_root, "add", "-A")
            self._git(worktree_root, "commit", "-m", self._objective_commit_message(objective_id, objective_title))
            commit_sha = self._git_output(worktree_root, "rev-parse", "HEAD").strip()
            self._run_pre_push_checks(worktree_root, source_repo_root=source_repo_root)

            if project.promotion_mode == PromotionMode.DIRECT_MAIN:
                self._git(worktree_root, "push", "origin", f"HEAD:{project.base_branch}")
                verified_remote_sha = self._verify_remote_sha(worktree_root, project.base_branch, commit_sha)
                push_result = PromotionApplyResult(
                    branch_name=branch_name,
                    commit_sha=commit_sha,
                    pushed_ref=f"{commit_sha}:{project.base_branch}",
                    cleanup_performed=False,
                    verified_remote_sha=verified_remote_sha,
                )
            else:
                self._git(worktree_root, "push", "-u", "origin", f"HEAD:{branch_name}")
                pr_url = None
                if project.promotion_mode == PromotionMode.BRANCH_AND_PR:
                    pseudo_task = Task(
                        id=objective_id,
                        project_id=project.id,
                        title=f"Promote objective {objective_title}",
                        objective=objective_title,
                    )
                    pr_url = self._open_review(project, pseudo_task, branch_name)
                push_result = PromotionApplyResult(
                    branch_name=branch_name,
                    commit_sha=commit_sha,
                    pushed_ref=branch_name,
                    pr_url=pr_url,
                    cleanup_performed=False,
                )
            return push_result
        finally:
            if worktree_added:
                self._cleanup_promotion_worktree(source_repo_root, worktree_root, branch_name)
                if push_result is not None:
                    push_result.cleanup_performed = True
            elif worktree_root.exists():
                shutil.rmtree(worktree_root, ignore_errors=True)

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
    def _objective_commit_message(objective_id: str, objective_title: str) -> str:
        return f"Promote objective {objective_title} ({objective_id})"

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

    @staticmethod
    def _normalize_objective_paths(objective_paths: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in objective_paths:
            path = str(raw or "").strip().replace("\\", "/")
            if not path or path.startswith("/") or path.startswith("../") or "/../" in f"/{path}":
                continue
            normalized = str(Path(path))
            if normalized == "." or normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(normalized)
        return sorted(cleaned)

    def _refresh_base_ref(self, source_repo_root: Path, base_branch: str) -> str:
        subprocess.run(
            ["git", "fetch", "origin", base_branch],
            cwd=source_repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        remote_ref = f"origin/{base_branch}"
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", remote_ref],
            cwd=source_repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            return remote_ref
        return base_branch

    def _verify_remote_sha(self, workspace_root: Path, base_branch: str, expected_sha: str) -> str:
        completed = subprocess.run(
            ["git", "ls-remote", "origin", f"refs/heads/{base_branch}"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
        remote_sha = completed.stdout.split()[0].strip() if completed.stdout.strip() else ""
        if remote_sha != expected_sha:
            raise RuntimeError(
                f"Remote verification failed for origin/{base_branch}: expected {expected_sha}, found {remote_sha or 'nothing'}"
            )
        return remote_sha

    def _sync_objective_paths(
        self,
        *,
        source_working_root: Path,
        worktree_root: Path,
        objective_paths: list[str],
    ) -> None:
        for relative_path in objective_paths:
            src = (source_working_root / relative_path).resolve()
            dest = (worktree_root / relative_path).resolve()
            if not str(dest).startswith(str(worktree_root) + os.sep) and dest != worktree_root:
                raise RuntimeError(f"Refusing to sync path outside worktree: {relative_path}")
            if src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(src, dest, dirs_exist_ok=True)
                else:
                    if dest.exists() and dest.is_dir():
                        shutil.rmtree(dest)
                    shutil.copy2(src, dest, follow_symlinks=False)
            elif dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()

    def _cleanup_promotion_worktree(self, source_repo_root: Path, worktree_root: Path, branch_name: str) -> None:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_root)],
            cwd=source_repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=source_repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if worktree_root.exists():
            shutil.rmtree(worktree_root, ignore_errors=True)

    def _run_pre_push_checks(self, workspace_root: Path, *, source_repo_root: Path | None = None) -> None:
        if not self.pre_push_commands:
            return
        if not (workspace_root / "Makefile").exists():
            return
        cleanup = self._ensure_pre_push_venv(workspace_root, source_repo_root=source_repo_root)
        try:
            for command in self.pre_push_commands:
                completed = subprocess.run(
                    list(command),
                    cwd=workspace_root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if completed.returncode != 0:
                    command_text = " ".join(command)
                    detail = (completed.stdout or completed.stderr or "").strip()
                    message = f"Pre-push verification failed for `{command_text}`."
                    if detail:
                        message = f"{message}\n\n{detail[-4000:]}"
                    raise RuntimeError(message)
        finally:
            cleanup()

    def _ensure_pre_push_venv(self, workspace_root: Path, *, source_repo_root: Path | None = None):
        venv_path = workspace_root / ".venv"
        if venv_path.exists():
            return lambda: None
        candidate = None
        if source_repo_root is not None:
            source_venv = source_repo_root / ".venv"
            if source_venv.exists():
                candidate = source_venv
        if candidate is None:
            return lambda: None
        os.symlink(candidate, venv_path, target_is_directory=True)
        return lambda: venv_path.unlink(missing_ok=True)
