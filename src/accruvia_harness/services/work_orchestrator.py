"""Skills-based work orchestrator.

Replaces the external worker CLI + report.json contract with a deterministic
pipeline over skills:

    /scope -> /implement -> (apply_changes) -> /self-review -> /validate -> (maybe /diagnose)

Each stage produces structured output that the next stage consumes. All
outputs are persisted as JSON artifacts on the run for audit and replay.
The final result conforms to the existing WorkResult contract so the rest
of run_service (analyzer, decider, promotion) continues to work unchanged.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain import Run, Task, new_id
from ..llm import LLMRouter
from ..policy import WorkResult
from ..skills import (
    CommitSkill,
    DiagnoseSkill,
    ImplementSkill,
    ScopeSkill,
    SelfReviewSkill,
    SkillInvocation,
    SkillRegistry,
    SkillResult,
    ValidateSkill,
    apply_changes,
    commands_for_profile,
    invoke_skill,
)
from ..skills.fix_tests import FixTestsSkill


@dataclass(slots=True)
class OrchestratorArtifact:
    kind: str
    path: Path
    summary: str


def _write_artifact(run_dir: Path, kind: str, payload: dict[str, Any], summary: str) -> OrchestratorArtifact:
    path = run_dir / f"{kind}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return OrchestratorArtifact(kind=kind, path=path, summary=summary)


def _to_artifact_tuples(artifacts: list[OrchestratorArtifact]) -> list[tuple[str, str, str]]:
    return [(a.kind, str(a.path), a.summary) for a in artifacts]


def _git_diff(workspace: Path) -> str:
    """Return unified diff of unstaged changes, *including* new untracked files.

    Uses `git add -N` (intent-to-add) on untracked files so `git diff` shows
    them. Without this, /self-review can't see newly created files and will
    infer they're missing — a false negative.

    Forces UTF-8 decoding with replacement because LLM-generated content often
    contains em-dashes, curly quotes, etc. that crash Windows cp1252 default.
    """
    run_kwargs = dict(capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        # Mark untracked files as intent-to-add so they show up in the diff.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            **run_kwargs,  # type: ignore[arg-type]
        )
        if status.returncode == 0:
            new_paths: list[str] = []
            for line in (status.stdout or "").splitlines():
                if line.startswith("?? "):
                    new_paths.append(line[3:].strip())
            if new_paths:
                subprocess.run(
                    ["git", "add", "-N", "--", *new_paths],
                    cwd=workspace,
                    **run_kwargs,  # type: ignore[arg-type]
                )
        result = subprocess.run(
            ["git", "diff", "--no-color"],
            cwd=workspace,
            **run_kwargs,  # type: ignore[arg-type]
        )
        if result.returncode == 0:
            return result.stdout or ""
        return ""
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return ""


def _collect_repo_context(workspace: Path, max_files: int = 80, max_bytes: int = 4000) -> str:
    """Build a short text listing of the workspace for scope context."""
    if not workspace.exists():
        return "(workspace missing)"
    lines: list[str] = []
    try:
        for path in sorted(workspace.rglob("*"))[:max_files * 4]:
            if not path.is_file():
                continue
            rel = path.relative_to(workspace).as_posix()
            # Skip hidden directories and common noise
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            if any(skip in rel for skip in ("node_modules/", "__pycache__/", "dist/", "build/")):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            lines.append(f"{rel} ({size} bytes)")
            if len(lines) >= max_files:
                break
    except OSError:
        return "(workspace listing failed)"
    text = "\n".join(lines)
    return text[:max_bytes] if text else "(empty workspace)"


def _load_file_contents(workspace: Path, paths: list[str], max_per_file: int = 40000) -> dict[str, str]:
    contents: dict[str, str] = {}
    for rel in paths:
        target = (workspace / rel.replace("\\", "/")).resolve()
        try:
            target.relative_to(workspace.resolve())
        except ValueError:
            continue
        if not target.exists() or not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        contents[rel] = text[:max_per_file]
    return contents


def _load_related_files(
    workspace: Path, objective: str, max_files: int = 5, max_total_bytes: int = 15000
) -> dict[str, str]:
    """Load contents of .py/.md files whose relative path appears in the objective."""
    if not workspace.exists() or not objective.strip():
        return {}
    objective_lower = objective.lower()
    candidates: list[Path] = []
    try:
        for path in sorted(workspace.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix not in (".py", ".md"):
                continue
            rel = path.relative_to(workspace).as_posix()
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            if any(skip in rel for skip in ("node_modules/", "__pycache__/", "dist/", "build/")):
                continue
            if rel.lower() in objective_lower:
                candidates.append(path)
                if len(candidates) >= max_files:
                    break
    except OSError:
        return {}
    contents: dict[str, str] = {}
    total = 0
    for path in candidates:
        rel = path.relative_to(workspace).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if total + len(text) > max_total_bytes:
            remaining = max_total_bytes - total
            if remaining <= 0:
                break
            text = text[:remaining]
        contents[rel] = text
        total += len(text)
    return contents


def _search_codebase(
    workspace: Path, queries: list[str], max_results_per_query: int = 10
) -> dict[str, list[str]]:
    """Run grep for each query in the workspace and return matching lines."""
    if not queries or not workspace.exists():
        return {}
    results: dict[str, list[str]] = {}
    run_kwargs = dict(capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    for query in queries:
        if not query.strip():
            continue
        try:
            proc = subprocess.run(
                ["grep", "-rnF", "--include=*.py", query, "."],
                cwd=workspace,
                **run_kwargs,  # type: ignore[arg-type]
            )
            lines = (proc.stdout or "").splitlines()[:max_results_per_query]
            if lines:
                results[query] = lines
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            continue
    return results


class SkillsWorkOrchestrator:
    """Runs scope/implement/self-review/validate over a single task + run."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        llm_router: LLMRouter,
        workspace_root: Path,
        telemetry: Any = None,
    ) -> None:
        self.skill_registry = skill_registry
        self.llm_router = llm_router
        self.workspace_root = Path(workspace_root)
        self.telemetry = telemetry

    def execute(
        self,
        task: Task,
        run: Run,
        workspace: Path,
        run_dir: Path,
        *,
        retry_feedback: str = "",
        prior_scope: dict[str, Any] | None = None,
    ) -> WorkResult:
        workspace = Path(workspace)
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[OrchestratorArtifact] = []

        scope_skill: ScopeSkill = self.skill_registry.get("scope")  # type: ignore[assignment]
        implement_skill: ImplementSkill = self.skill_registry.get("implement")  # type: ignore[assignment]
        self_review_skill: SelfReviewSkill = self.skill_registry.get("self_review")  # type: ignore[assignment]
        validate_skill: ValidateSkill = self.skill_registry.get("validate")  # type: ignore[assignment]
        diagnose_skill: DiagnoseSkill = self.skill_registry.get("diagnose")  # type: ignore[assignment]
        commit_skill: CommitSkill = self.skill_registry.get("commit")  # type: ignore[assignment]

        fix_tests_skill = FixTestsSkill()
        repo_context = _collect_repo_context(workspace)
        related_file_contents = _load_related_files(workspace, task.objective)

        # Extract search queries from objective for codebase grep
        _query_matches = re.findall(
            r'"([^"]+)"|([A-Z][a-z]+(?:[A-Z][a-z]+)+)', task.objective
        )
        _search_queries = [m[0] or m[1] for m in _query_matches if m[0] or m[1]]
        codebase_search_results = _search_codebase(workspace, _search_queries)

        # STAGE 1: /scope
        scope_result = invoke_skill(
            scope_skill,
            SkillInvocation(
                skill_name="scope",
                inputs={
                    "title": task.title,
                    "objective": task.objective,
                    "strategy": task.strategy,
                    "allowed_paths": (task.scope or {}).get("allowed_paths") or [],
                    "forbidden_paths": (task.scope or {}).get("forbidden_paths") or [],
                    "repo_context": repo_context,
                    "related_file_contents": related_file_contents,
                    "codebase_search_results": codebase_search_results,
                    "prior_scope": prior_scope,
                    "retry_feedback": retry_feedback,
                },
                task=task,
                run=run,
                run_dir=run_dir / "skill_scope",
            ),
            self.llm_router,
            telemetry=self.telemetry,
        )
        artifacts.append(
            _write_artifact(
                run_dir, "scope_output",
                {"success": scope_result.success, "errors": scope_result.errors, "output": scope_result.output},
                "Structured scope decision",
            )
        )
        if not scope_result.success:
            return WorkResult(
                summary="Scope skill failed to produce valid output.",
                artifacts=_to_artifact_tuples(artifacts),
                outcome="failed",
                diagnostics={
                    "stage": "scope",
                    "worker_outcome": "failed",
                    "worker_backend": "skills",
                    "failure_category": "scope_skill_failure",
                    "failure_message": "; ".join(scope_result.errors),
                    "validation_profile": task.validation_profile,
                },
            )
        scope = scope_result.output
        if scope.get("estimated_complexity") == "too_large":
            return WorkResult(
                summary="Scope flagged task as too large; split before implementing.",
                artifacts=_to_artifact_tuples(artifacts),
                outcome="blocked",
                diagnostics={
                    "stage": "scope",
                    "worker_outcome": "blocked",
                    "worker_backend": "skills",
                    "failure_category": "scope_too_broad",
                    "failure_message": scope.get("approach", "Scope marked too_large"),
                    "needs_split": True,
                    "validation_profile": task.validation_profile,
                },
            )

        # STAGE 2: /implement
        files_to_touch = list(scope.get("files_to_touch") or [])
        file_contents = _load_file_contents(workspace, files_to_touch)
        # Load reference files: imports from target files + related test files.
        # This gives /implement the context it needs for integration edits.
        reference_contents = _load_reference_contents(
            workspace, files_to_touch, file_contents,
        )
        implement_result = invoke_skill(
            implement_skill,
            SkillInvocation(
                skill_name="implement",
                inputs={
                    "title": task.title,
                    "objective": task.objective,
                    "approach": scope.get("approach", ""),
                    "files_to_touch": files_to_touch,
                    "files_not_to_touch": scope.get("files_not_to_touch") or [],
                    "risks": scope.get("risks") or [],
                    "file_contents": file_contents,
                    "reference_contents": reference_contents,
                    "retry_feedback": retry_feedback,
                },
                task=task,
                run=run,
                run_dir=run_dir / "skill_implement",
            ),
            self.llm_router,
            telemetry=self.telemetry,
        )
        artifacts.append(
            _write_artifact(
                run_dir, "implementation_output",
                {"success": implement_result.success, "errors": implement_result.errors, "output": implement_result.output},
                "Structured implementation output",
            )
        )
        if not implement_result.success:
            return WorkResult(
                summary="Implement skill failed to produce valid output.",
                artifacts=_to_artifact_tuples(artifacts),
                outcome="failed",
                diagnostics={
                    "stage": "implement",
                    "worker_outcome": "failed",
                    "worker_backend": "skills",
                    "failure_category": "implement_skill_failure",
                    "failure_message": "; ".join(implement_result.errors),
                    "validation_profile": task.validation_profile,
                },
            )

        apply_summary = apply_changes(
            implement_result,
            workspace_root=workspace,
            allowed_files=list(scope.get("files_to_touch") or []),
        )
        artifacts.append(
            _write_artifact(
                run_dir, "apply_changes_summary", apply_summary,
                "File-apply audit",
            )
        )
        if apply_summary["rejected"] and not apply_summary["written"]:
            return WorkResult(
                summary="All proposed file writes were rejected as out of scope.",
                artifacts=_to_artifact_tuples(artifacts),
                outcome="failed",
                diagnostics={
                    "stage": "apply_changes",
                    "worker_outcome": "failed",
                    "worker_backend": "skills",
                    "failure_category": "scope_violation",
                    "failure_message": f"Rejected: {apply_summary['rejected']}",
                    "validation_profile": task.validation_profile,
                },
            )

        # STAGE 3: /self-review
        diff_text = _git_diff(workspace)
        self_review_result = invoke_skill(
            self_review_skill,
            SkillInvocation(
                skill_name="self_review",
                inputs={
                    "title": task.title,
                    "objective": task.objective,
                    "approach": scope.get("approach", ""),
                    "risks": scope.get("risks") or [],
                    "diff": diff_text,
                },
                task=task,
                run=run,
                run_dir=run_dir / "skill_self_review",
            ),
            self.llm_router,
            telemetry=self.telemetry,
        )
        artifacts.append(
            _write_artifact(
                run_dir, "self_review_output",
                {"success": self_review_result.success, "errors": self_review_result.errors, "output": self_review_result.output},
                "Staff-engineer self-review",
            )
        )
        ship_ready = bool(self_review_result.output.get("ship_ready")) if self_review_result.success else False

        # STAGE 4: /validate (deterministic)
        profile = task.validation_profile or "generic"
        written_files = apply_summary.get("written") or []
        commands = _resolve_validate_commands(
            profile=profile,
            validation_mode=task.validation_mode,
            changed_files=written_files,
            workspace=workspace,
        )
        validate_result = validate_skill.invoke_deterministic(
            workspace_root=workspace,
            commands=commands,
            run_dir=run_dir / "skill_validate",
        )
        artifacts.append(
            _write_artifact(
                run_dir, "validation_output", validate_result.output,
                "Deterministic compile+test validation",
            )
        )
        overall = str(validate_result.output.get("overall") or "skipped")

        # STAGE 5 (conditional): /diagnose when validation fails
        diagnosis: dict[str, Any] | None = None
        if overall == "fail":
            evidence = str(validate_result.output.get("failure_evidence") or "")
            diag_result = invoke_skill(
                diagnose_skill,
                SkillInvocation(
                    skill_name="diagnose",
                    inputs={
                        "evidence": evidence,
                        "context": f"task: {task.title}; objective: {task.objective}",
                        "attempt": run.attempt,
                    },
                    task=task,
                    run=run,
                    run_dir=run_dir / "skill_diagnose",
                ),
                self.llm_router,
                telemetry=self.telemetry,
            )
            if diag_result.success:
                diagnosis = diag_result.output
            artifacts.append(
                _write_artifact(
                    run_dir, "diagnosis_output",
                    {"success": diag_result.success, "errors": diag_result.errors, "output": diag_result.output},
                    "Diagnosed validation failure",
                )
            )

        # STAGE 5.5 (conditional): iterative /fix-tests loop
        # When validation fails on test assertions (not syntax/import errors),
        # try up to 3 rounds of: get verbose failure → /fix-tests → re-validate.
        _MAX_FIX_ROUNDS = 3
        changed_test_files = [p for p in (apply_summary.get("written") or []) if _is_test_path(p)]
        if (
            overall == "fail"
            and diagnosis is not None
            and diagnosis.get("classification") in {"code_defect", "test_infrastructure_failure"}
            and _looks_like_test_assertion_failure(validate_result.output)
            and changed_test_files
        ):
            for fix_round in range(1, _MAX_FIX_ROUNDS + 1):
                # Re-run validation with verbose output for better diagnostics
                verbose_commands = _verbose_test_commands(commands)
                verbose_result = validate_skill.invoke_deterministic(
                    workspace_root=workspace,
                    commands=verbose_commands,
                    run_dir=run_dir / f"skill_validate_verbose_{fix_round}",
                )
                verbose_evidence = str(verbose_result.output.get("failure_evidence") or "")

                fix_result = self._try_fix_tests(
                    task=task,
                    run=run,
                    run_dir=run_dir,
                    workspace=workspace,
                    diff_text=diff_text,
                    failure_evidence=verbose_evidence or str(validate_result.output.get("failure_evidence") or ""),
                    changed_test_files=changed_test_files,
                    artifacts=artifacts,
                    round_number=fix_round,
                )
                if fix_result is None or not fix_result.success:
                    break
                # Re-validate after fixes
                revalidate = validate_skill.invoke_deterministic(
                    workspace_root=workspace,
                    commands=commands,
                    run_dir=run_dir / f"skill_validate_retry_{fix_round}",
                )
                artifacts.append(
                    _write_artifact(
                        run_dir, f"validation_retry_{fix_round}_output", revalidate.output,
                        f"Re-validation after /fix-tests round {fix_round}",
                    )
                )
                retry_overall = str(revalidate.output.get("overall") or "fail")
                if retry_overall != "fail":
                    overall = retry_overall
                    validate_result = revalidate
                    diagnosis = None
                    break
                # Still failing — loop continues with fresh verbose diagnostics

        # Compose final WorkResult
        written = apply_summary.get("written") or []
        test_files = [p for p in written if _is_test_path(p)]
        # Synthesize a consolidated report artifact so tasks that require
        # `report` see a satisfied artifact. Aggregates all skill outputs.
        artifacts.append(
            _write_artifact(
                run_dir, "report",
                {
                    "worker_backend": "skills",
                    "worker_outcome": "candidate" if overall != "fail" and ship_ready else "failed",
                    "validation_profile": profile,
                    "changed_files": written,
                    "test_files": test_files,
                    "compile_check": {"passed": _stage_passed(validate_result.output, "compile") or _stage_passed(validate_result.output, "build") or overall == "skipped"},
                    "test_check": {"passed": _stage_passed(validate_result.output, "tests") or _stage_passed(validate_result.output, "test") or overall == "skipped"},
                    "ship_ready": ship_ready,
                    "overall_validation": overall,
                    "scope": scope,
                    "implementation_rationale": implement_result.output.get("rationale", ""),
                    "self_review_summary": self_review_result.output.get("summary", "") if self_review_result.success else "",
                    "diagnosis": diagnosis,
                },
                "Skills-pipeline consolidated report",
            )
        )
        if overall == "fail":
            fail_diagnostics: dict[str, Any] = {
                "stage": "validate",
                "worker_outcome": "failed",
                "worker_backend": "skills",
                "failure_category": diagnosis.get("classification") if diagnosis else "validation_failure",
                "failure_message": diagnosis.get("root_cause") if diagnosis else str(validate_result.output.get("failure_evidence") or "")[:500],
                "validation_profile": profile,
                "changed_files": written,
                "diagnosis": diagnosis,
                "compile_check": {"passed": _stage_passed(validate_result.output, "compile") or _stage_passed(validate_result.output, "build")},
                "test_check": {"passed": _stage_passed(validate_result.output, "tests") or _stage_passed(validate_result.output, "test")},
            }
            if diagnosis and diagnosis.get("scope_adjustment"):
                fail_diagnostics["retry_hints"] = {
                    "review_feedback": diagnosis["scope_adjustment"],
                    "prior_scope": scope,
                }
            return WorkResult(
                summary=f"Validation failed: {diagnosis.get('root_cause', 'see evidence') if diagnosis else 'see evidence'}",
                artifacts=_to_artifact_tuples(artifacts),
                outcome="failed",
                diagnostics=fail_diagnostics,
            )
        if not ship_ready:
            review_feedback = SelfReviewSkill.feedback_for_retry(self_review_result)
            return WorkResult(
                summary="Self-review blocked shipping; retry with feedback.",
                artifacts=_to_artifact_tuples(artifacts),
                outcome="failed",
                diagnostics={
                    "stage": "self_review",
                    "worker_outcome": "failed",
                    "worker_backend": "skills",
                    "failure_category": "self_review_blocked",
                    "failure_message": review_feedback[:500],
                    "review_feedback": review_feedback,
                    "validation_profile": profile,
                    "changed_files": written,
                },
            )
        # STAGE 6: /commit â€” persist changes in git
        deleted_files = implement_result.output.get("deleted_files") or []
        commit_paths = list(written) + [p for p in deleted_files if p not in written]
        rationale = implement_result.output.get("rationale") or ""
        commit_message = (
            f"Task: {task.title}\n\n"
            f"{rationale[:500]}\n\n"
            f"Authored by skills pipeline: {run.id}"
        )
        commit_result = commit_skill.invoke_deterministic(
            workspace=workspace,
            paths=commit_paths,
            message=commit_message,
            author_name="Accruvia Harness",
            author_email="harness@accruvia.local",
        )
        artifacts.append(
            _write_artifact(
                run_dir, "commit_output",
                {"success": commit_result.success, "errors": commit_result.errors, "output": commit_result.output},
                "Git commit of validated changes",
            )
        )
        final_diagnostics: dict[str, Any] = {
            "stage": "complete",
            "worker_outcome": "candidate",
            "worker_backend": "skills",
            "skip_external_validation": True,
            "validation_profile": profile,
            "changed_files": written,
            "compile_check": {"passed": _stage_passed(validate_result.output, "compile") or _stage_passed(validate_result.output, "build") or overall == "skipped"},
            "test_check": {"passed": _stage_passed(validate_result.output, "tests") or _stage_passed(validate_result.output, "test") or overall == "skipped"},
            "test_files": [p for p in written if "test" in p.lower()],
        }
        if commit_result.success:
            final_diagnostics["commit_sha"] = commit_result.output.get("commit_sha") or ""
        else:
            final_diagnostics["commit_error"] = (
                "; ".join(commit_result.errors) if commit_result.errors else "commit failed"
            )
        return WorkResult(
            summary=implement_result.output.get("rationale") or "Task implemented and validated.",
            artifacts=_to_artifact_tuples(artifacts),
            outcome="success",
            diagnostics=final_diagnostics,
        )


    def _try_fix_tests(
        self,
        *,
        task: Task,
        run: Run,
        run_dir: Path,
        workspace: Path,
        diff_text: str,
        failure_evidence: str,
        changed_test_files: list[str],
        artifacts: list[OrchestratorArtifact],
        round_number: int = 1,
    ) -> SkillResult | None:
        """Attempt to fix test assertions via /fix-tests skill.

        Returns the skill result if fix was attempted, None if not applicable.
        Applies edits to the workspace directly (reusing apply_changes).
        """
        if not changed_test_files:
            return None
        # Use the first failing test file for context
        test_path = changed_test_files[0]
        test_full = (workspace / test_path).resolve()
        if not test_full.exists():
            return None
        try:
            test_content = test_full.read_text(encoding="utf-8")[:40000]
        except OSError:
            return None

        fix_result = invoke_skill(
            FixTestsSkill(),
            SkillInvocation(
                skill_name="fix_tests",
                inputs={
                    "title": task.title,
                    "objective": task.objective,
                    "diff": diff_text[:8000],
                    "failure_output": failure_evidence[:8000],
                    "test_file_path": test_path,
                    "test_file_content": test_content,
                },
                task=task,
                run=run,
                run_dir=run_dir / f"skill_fix_tests_{round_number}",
            ),
            self.llm_router,
            telemetry=self.telemetry,
        )
        artifacts.append(
            _write_artifact(
                run_dir, f"fix_tests_{round_number}_output",
                {"success": fix_result.success, "errors": fix_result.errors, "output": fix_result.output},
                f"Test assertion fixes from /fix-tests (round {round_number})",
            )
        )
        if not fix_result.success:
            return fix_result
        # Apply the test edits. Only the test file is allowed.
        fix_apply = apply_changes(
            fix_result,
            workspace_root=workspace,
            allowed_files=changed_test_files,
        )
        artifacts.append(
            _write_artifact(
                run_dir, f"fix_tests_{round_number}_apply", fix_apply,
                f"Apply /fix-tests edits (round {round_number})",
            )
        )
        return fix_result


def _parse_imports(content: str) -> list[str]:
    """Extract imported module paths from Python source. Returns dotted names."""
    import re

    modules: list[str] = []
    for match in re.finditer(r"^\s*(?:from|import)\s+([\w.]+)", content, re.MULTILINE):
        modules.append(match.group(1))
    return modules


def _module_to_path(module: str, workspace: Path) -> Path | None:
    """Best-effort: convert dotted module to a file path under workspace."""
    parts = module.replace(".", "/")
    for prefix in ("src/", ""):
        candidate = workspace / prefix / (parts + ".py")
        if candidate.exists():
            return candidate
        candidate = workspace / prefix / parts / "__init__.py"
        if candidate.exists():
            return candidate
    return None


def _load_reference_contents(
    workspace: Path,
    files_to_touch: list[str],
    file_contents: dict[str, str],
    *,
    max_ref_files: int = 8,
    max_bytes_per_file: int = 20000,
    max_total_bytes: int = 80000,
) -> dict[str, str]:
    """Load read-only reference files that /implement needs for integration context.

    Includes:
    1. Modules imported by files_to_touch (API signatures, domain types)
    2. Test files that import files_to_touch (fixture patterns)
    """
    seen: set[str] = set(files_to_touch)
    candidates: list[Path] = []

    # Collect imports from each target file
    for content in file_contents.values():
        for module in _parse_imports(content):
            path = _module_to_path(module, workspace)
            if path is not None:
                rel = path.relative_to(workspace.resolve()).as_posix()
                if rel not in seen:
                    seen.add(rel)
                    candidates.append(path)

    # Collect test files that import target modules
    source_files = [
        f for f in files_to_touch
        if f.endswith(".py") and not _is_test_path(f)
    ]
    if source_files:
        importing = _find_importing_tests(workspace, source_files, max_additional=5)
        for test_rel in importing:
            if test_rel not in seen:
                seen.add(test_rel)
                candidates.append((workspace / test_rel).resolve())

    # Load up to budget
    result: dict[str, str] = {}
    total = 0
    for path in candidates[:max_ref_files]:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        budget = min(max_bytes_per_file, max_total_bytes - total)
        if budget <= 0:
            break
        text = text[:budget]
        total += len(text)
        rel = path.relative_to(workspace.resolve()).as_posix()
        result[rel] = text
    return result


def _verbose_test_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace pytest flags with verbose equivalents for better diagnostics."""
    result: list[dict[str, Any]] = []
    for entry in commands:
        cmd = str(entry.get("cmd") or "")
        if "pytest" in cmd:
            # Replace -q/--no-header/-x with -vv --tb=long for full tracebacks
            verbose_cmd = cmd.replace("-q", "-vv").replace("--no-header", "--tb=long")
            if "-vv" not in verbose_cmd:
                verbose_cmd += " -vv --tb=long"
            result.append({**entry, "cmd": verbose_cmd})
        else:
            result.append(entry)
    return result


def _looks_like_test_assertion_failure(validation_output: dict[str, Any]) -> bool:
    """Heuristic: does the failure evidence look like a test assertion mismatch
    rather than a compilation error or infrastructure issue?"""
    evidence = str(validation_output.get("failure_evidence") or "").lower()
    assertion_signals = ("assertionerror", "assert ", "failed", "expected", "!=", "assertequal", "asserttrue", "assertin")
    infra_signals = ("modulenotfounderror", "importerror", "syntaxerror", "indentationerror")
    has_assertion = any(sig in evidence for sig in assertion_signals)
    has_infra = any(sig in evidence for sig in infra_signals)
    return has_assertion and not has_infra


def _stage_passed(validation_output: dict[str, Any], stage_name: str) -> bool:
    for entry in validation_output.get("results") or []:
        if str(entry.get("name")) == stage_name and str(entry.get("status")) == "pass":
            return True
    return False


def _is_test_path(path: str) -> bool:
    norm = path.replace("\\", "/")
    basename = norm.rsplit("/", 1)[-1]
    return (
        norm.startswith("tests/")
        or "/tests/" in norm
        or basename.startswith("test_")
        or basename.endswith("_test.py")
    )


def _find_importing_tests(workspace: Path, changed_source_files: list[str], max_additional: int = 10) -> list[str]:
    """Find test files that import any of the changed source modules.

    Runs pytest --collect-only to discover test files, then searches them
    for import statements matching changed source modules.
    """
    if not changed_source_files or not workspace.exists():
        return []
    module_names: list[str] = []
    for src_path in changed_source_files:
        norm = src_path.replace("\\", "/")
        if norm.startswith("src/"):
            norm = norm[4:]
        if norm.endswith(".py"):
            norm = norm[:-3]
        if norm.endswith("/__init__"):
            norm = norm[:-9]
        module_name = norm.replace("/", ".")
        if module_name:
            module_names.append(module_name)
    if not module_names:
        return []
    run_kwargs = dict(capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "--collect-only", "-q"],
            cwd=workspace,
            **run_kwargs,  # type: ignore[arg-type]
        )
        output = result.stdout or ""
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return []
    test_files: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        if "::" in line:
            test_file = line.split("::")[0]
            if test_file.endswith(".py"):
                test_files.add(test_file.replace("\\", "/"))
    matched: list[str] = []
    for test_file in sorted(test_files):
        test_path = workspace / test_file
        if not test_path.is_file():
            continue
        try:
            content = test_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for mod in module_names:
            if f"import {mod}" in content or f"from {mod}" in content:
                matched.append(test_file)
                break
        if len(matched) >= max_additional:
            break
    return matched


def _resolve_validate_commands(
    *,
    profile: str,
    validation_mode: str | None,
    changed_files: list[str],
    workspace: Path | None = None,
) -> list[dict[str, Any]]:
    """Build the validation command list for the work orchestrator.

    - validation_mode=lightweight_operator => no commands (UX/DX tweaks)
    - python profile with changed test files => pytest scoped to those files
    - otherwise => profile defaults from commands_for_profile
    """
    if (validation_mode or "").strip() == "lightweight_operator":
        return []
    default_commands = commands_for_profile(profile)
    if profile != "python":
        return default_commands
    test_files = [p for p in changed_files if _is_test_path(p) and p.endswith(".py")]
    if workspace is not None:
        source_files = [
            p for p in changed_files
            if p.endswith(".py") and not _is_test_path(p)
        ]
        if source_files:
            importing_tests = _find_importing_tests(workspace, source_files)
            seen = set(test_files)
            for t in importing_tests:
                if t not in seen:
                    test_files.append(t)
                    seen.add(t)
    if not test_files:
        return default_commands
    # Quote each test file path and run pytest against just those.
    quoted = " ".join(f'"{path}"' for path in test_files)
    return [
        {"name": "compile", "cmd": "python -m compileall -q .", "timeout": 120},
        {
            "name": "tests",
            "cmd": f"python -m pytest -q --no-header -x {quoted}",
            "timeout": 600,
        },
    ]
