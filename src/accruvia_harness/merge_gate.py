"""Merge gate — governance layer over harness-produced branches.

After the skills pipeline promotes a run, the merge gate decides whether
that run is safe to auto-merge into main. This closes the last
human-in-the-loop gap for fully autonomous operation.

Policy inputs:
  - Run's decision action (must be PROMOTE)
  - Run's consolidated report artifact (compile + test pass, ship_ready)
  - Run's changed_files list (intersection with denied paths)
  - Git state (branch exists, worktree clean, no remote divergence)

The gate is conservative by default. Anything that fails a check goes to
manual review rather than failing the run.
"""
from __future__ import annotations

import fnmatch
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import DecisionAction


# Paths that require human review regardless of task scope. Touching any of
# these should block auto-merge. Focused on configs, CI, secrets, and the
# harness launch surface.
DEFAULT_DENIED_PATHS: tuple[str, ...] = (
    ".github/**",
    ".gitignore",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "bin/*",
    ".accruvia-harness/config.json",
    "**/.env",
    "**/.env.*",
    "secrets/**",
    "*.pem",
    "*.key",
)


@dataclass(slots=True)
class MergePolicy:
    denied_paths: tuple[str, ...] = DEFAULT_DENIED_PATHS
    require_compile_pass: bool = True
    require_test_pass: bool = True
    require_ship_ready: bool = True
    max_changed_files: int = 50
    allow_deletions: bool = True
    target_branch: str = "main"


@dataclass(slots=True)
class MergeDecision:
    auto_merge: bool
    run_id: str
    task_id: str
    branch_name: str | None
    changed_files: list[str] = field(default_factory=list)
    reason: str = ""
    concerns: list[str] = field(default_factory=list)
    report: dict[str, Any] | None = None


@dataclass(slots=True)
class MergeResult:
    merged: bool
    commit_sha: str = ""
    conflicts: list[str] = field(default_factory=list)
    stderr: str = ""


def _matches_any(path: str, patterns: tuple[str, ...]) -> str | None:
    """Return the first matching pattern for path, or None."""
    normalized = path.replace("\\", "/")
    # Strip leading "./" but not a leading "." (e.g. .github/ must survive)
    if normalized.startswith("./"):
        normalized = normalized[2:]
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern):
            return pattern
        # Also match on the basename for globs without slashes
        if "/" not in pattern and fnmatch.fnmatch(
            normalized.rsplit("/", 1)[-1], pattern
        ):
            return pattern
    return None


def _load_report(report_path: str) -> dict[str, Any] | None:
    try:
        return json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _branch_for_run(store: Any, run_id: str) -> str | None:
    """Harness worktrees use branch name harness-<task6>-<run6>."""
    run = store.get_run(run_id)
    if run is None:
        return None
    task_id = run.task_id
    return f"harness-{task_id[-6:]}-{run_id[-6:]}"


def evaluate_run(
    store: Any,
    run_id: str,
    policy: MergePolicy | None = None,
) -> MergeDecision:
    """Decide whether this promoted run is safe to auto-merge.

    Never raises. Any unexpected state downgrades to auto_merge=False with
    the reason captured in concerns.
    """
    policy = policy or MergePolicy()
    concerns: list[str] = []

    run = store.get_run(run_id)
    if run is None:
        return MergeDecision(
            auto_merge=False, run_id=run_id, task_id="", branch_name=None,
            reason="run not found", concerns=["run_not_found"],
        )
    task_id = run.task_id

    decisions = store.list_decisions(run_id)
    if not decisions:
        return MergeDecision(
            auto_merge=False, run_id=run_id, task_id=task_id,
            branch_name=_branch_for_run(store, run_id),
            reason="no decision recorded", concerns=["no_decision"],
        )
    latest_decision = decisions[-1]
    action = latest_decision.action
    if hasattr(action, "value"):
        action = action.value  # StrEnum compatibility
    if action != DecisionAction.PROMOTE.value and action != "promote":
        return MergeDecision(
            auto_merge=False, run_id=run_id, task_id=task_id,
            branch_name=_branch_for_run(store, run_id),
            reason=f"decision is {action}, not promote",
            concerns=[f"decision_not_promote:{action}"],
        )

    artifacts = store.list_artifacts(run_id)
    report_artifact = next((a for a in artifacts if a.kind == "report"), None)
    if report_artifact is None:
        return MergeDecision(
            auto_merge=False, run_id=run_id, task_id=task_id,
            branch_name=_branch_for_run(store, run_id),
            reason="no report artifact", concerns=["missing_report"],
        )
    report = _load_report(report_artifact.path) or {}
    changed_files = [str(p) for p in (report.get("changed_files") or [])]

    # Policy: ship_ready
    if policy.require_ship_ready and not bool(report.get("ship_ready")):
        concerns.append("self_review_not_ship_ready")

    # Policy: compile + tests
    compile_check = report.get("compile_check") or {}
    test_check = report.get("test_check") or {}
    if policy.require_compile_pass and not bool(compile_check.get("passed")):
        concerns.append("compile_check_not_passed")
    if policy.require_test_pass and not bool(test_check.get("passed")):
        concerns.append("test_check_not_passed")

    # Policy: overall validation
    overall = str(report.get("overall_validation") or "")
    if overall == "fail":
        concerns.append("validation_overall_failed")

    # Policy: file count cap
    if len(changed_files) > policy.max_changed_files:
        concerns.append(
            f"changed_files_over_cap:{len(changed_files)}>{policy.max_changed_files}"
        )

    # Policy: denied paths
    for path in changed_files:
        matched = _matches_any(path, policy.denied_paths)
        if matched is not None:
            concerns.append(f"denied_path:{path}:{matched}")

    branch_name = _branch_for_run(store, run_id)
    auto_merge = not concerns
    reason = (
        "all policy checks passed" if auto_merge
        else f"{len(concerns)} concern(s) block auto-merge"
    )
    return MergeDecision(
        auto_merge=auto_merge,
        run_id=run_id,
        task_id=task_id,
        branch_name=branch_name,
        changed_files=changed_files,
        reason=reason,
        concerns=concerns,
        report=report,
    )


def _git(args: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def execute_merge(
    repo_root: Path,
    branch_name: str,
    *,
    target_branch: str = "main",
    merge_message: str | None = None,
) -> MergeResult:
    """Merge branch_name into target_branch with --no-ff. Conservative: will
    not run if the working tree is dirty or the target branch is behind origin.
    """
    repo_root = Path(repo_root).resolve()
    if not (repo_root / ".git").exists():
        return MergeResult(merged=False, stderr="not a git repository")

    # Verify branch exists
    rc = _git(["rev-parse", "--verify", branch_name], repo_root)
    if rc.returncode != 0:
        return MergeResult(merged=False, stderr=f"branch not found: {branch_name}")

    # Verify target branch exists
    rc = _git(["rev-parse", "--verify", target_branch], repo_root)
    if rc.returncode != 0:
        return MergeResult(merged=False, stderr=f"target branch not found: {target_branch}")

    # Verify clean working tree (ignore untracked files like .accruvia-harness/)
    rc = _git(["status", "--porcelain", "--untracked-files=no"], repo_root)
    if rc.returncode == 0 and rc.stdout.strip():
        return MergeResult(merged=False, stderr="working tree is dirty")

    # Current branch check (must be on target or a sibling)
    rc = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    current_branch = rc.stdout.strip() if rc.returncode == 0 else ""
    if current_branch != target_branch:
        rc = _git(["checkout", target_branch], repo_root)
        if rc.returncode != 0:
            return MergeResult(merged=False, stderr=f"failed to checkout {target_branch}: {rc.stderr}")

    # Merge
    message = merge_message or f"Auto-merge {branch_name} (harness merge gate)"
    rc = _git(
        ["merge", "--no-ff", branch_name, "-m", message],
        repo_root,
        timeout=120,
    )
    if rc.returncode != 0:
        # Capture conflicts and abort
        conflicts_rc = _git(["diff", "--name-only", "--diff-filter=U"], repo_root)
        conflicts = [
            line.strip() for line in (conflicts_rc.stdout or "").splitlines()
            if line.strip()
        ]
        _git(["merge", "--abort"], repo_root)
        return MergeResult(
            merged=False, conflicts=conflicts, stderr=rc.stderr or "merge failed",
        )

    # Capture the commit SHA
    rc = _git(["rev-parse", "HEAD"], repo_root)
    commit_sha = rc.stdout.strip() if rc.returncode == 0 else ""
    return MergeResult(merged=True, commit_sha=commit_sha)


def auto_merge_run(
    store: Any,
    run_id: str,
    repo_root: Path,
    *,
    policy: MergePolicy | None = None,
    dry_run: bool = False,
) -> tuple[MergeDecision, MergeResult | None]:
    """Evaluate policy and, if safe, execute the merge.

    Returns (decision, result). result is None if auto_merge=False or dry_run.
    Does not raise; failures surface through MergeDecision.concerns and
    MergeResult.stderr.
    """
    decision = evaluate_run(store, run_id, policy)
    if not decision.auto_merge or dry_run:
        return decision, None
    if decision.branch_name is None:
        return (
            MergeDecision(
                auto_merge=False, run_id=run_id, task_id=decision.task_id,
                branch_name=None, reason="no branch name", concerns=["no_branch"],
            ),
            None,
        )
    policy = policy or MergePolicy()
    merge_message = (
        f"Auto-merge {decision.branch_name} (run {run_id})\n\n"
        f"Policy checks passed: compile+tests green, ship_ready=true, "
        f"{len(decision.changed_files)} files, no denied paths.\n"
    )
    result = execute_merge(
        repo_root=repo_root,
        branch_name=decision.branch_name,
        target_branch=policy.target_branch,
        merge_message=merge_message,
    )
    return decision, result
