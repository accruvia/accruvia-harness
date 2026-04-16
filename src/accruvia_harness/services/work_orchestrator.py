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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cost_tracker import CostTracker
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
        self.progress_callback = None

    def _emit(self, skill_name: str, status: str, detail: str = "") -> None:
        """Emit a progress event so the CLI shows per-skill status."""
        if self.progress_callback is not None:
            self.progress_callback({
                "type": "worker_phase",
                "worker_phase": skill_name,
                "status": status,
                "detail": detail,
            })

    def _span(self, name: str, **attributes: Any):
        """Open a telemetry span when a telemetry sink is wired.

        Returns a context manager in both cases. When telemetry is None
        (most tests, bootstrap paths) the returned object is a no-op
        context so the `with` blocks inside execute() don't branch on
        telemetry presence. This gives observers a per-stage and total-
        pipeline view of skills execution — see specs/split-phase-execution.md
        for why stage-level timing matters. Span names follow the
        'skills_<stage>' convention so dashboards can filter the skill
        pipeline out from other harness timing events.
        """
        from contextlib import nullcontext

        if self.telemetry is None:
            return nullcontext()
        return self.telemetry.timed(name, **attributes)

    def _emit_stage_span(self, stage: str, start_time: float, **attributes: Any) -> None:
        """Record a finished stage as a telemetry span.

        Used at stage boundaries inside execute() where wrapping the whole
        body in a `with` block would force a large indent rewrite. Start
        time is captured at stage begin via `time.perf_counter()` and
        passed here at stage end. No-op when telemetry is None.
        """
        if self.telemetry is None:
            return
        import time as _time

        duration_ms = (_time.perf_counter() - start_time) * 1000
        span_name = f"skills_{stage}"
        self.telemetry.span(span_name, duration_ms=duration_ms, **attributes)
        self.telemetry.metric(
            f"{span_name}_duration_ms", duration_ms, metric_type="histogram", **attributes,
        )

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
        with self._span("skills_pipeline", task_id=task.id, run_id=run.id, attempt=run.attempt):
            return self._execute(task, run, workspace, run_dir, retry_feedback=retry_feedback, prior_scope=prior_scope)

    def _execute(
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

        # Pre-flight budget check: block LLM-dependent tasks if daily spend exceeded
        if task.validation_mode != "lightweight_operator":
            cost_tracker = CostTracker()
            within_budget, _remaining = cost_tracker.check_budget(task.project_id)
            if not within_budget:
                return WorkResult(
                    summary="Daily LLM budget exceeded; task blocked.",
                    artifacts=_to_artifact_tuples(artifacts),
                    outcome="blocked",
                    diagnostics={
                        "failure_category": "budget_exhausted",
                        "failure_message": "Daily LLM budget exceeded. Deterministic tasks can still run.",
                    },
                )

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

        # STAGE 1: /scope — skip LLM call when task.scope already has
        # files_to_touch + approach (set at task creation from TRIO plans).
        _scope_start = time.perf_counter()
        _task_scope = task.scope or {}
        _has_precomputed_scope = (
            _task_scope.get("files_to_touch")
            and _task_scope.get("approach")
        )
        if _has_precomputed_scope:
            self._emit("scope", "running", "using plan scope (no LLM call)")
            scope = {
                "files_to_touch": list(_task_scope["files_to_touch"]),
                "files_not_to_touch": list(_task_scope.get("files_not_to_touch") or []),
                "approach": str(_task_scope["approach"]),
                "risks": list(_task_scope.get("risks") or []),
                "estimated_complexity": str(
                    _task_scope.get("estimated_complexity") or "medium"
                ),
            }
            artifacts.append(
                _write_artifact(
                    run_dir, "scope_output",
                    {"success": True, "errors": [], "output": scope},
                    "Scope from plan (no LLM call)",
                )
            )
            artifacts.append(
                _write_artifact(
                    run_dir, "plan", scope,
                    "Plan-derived scope (approach, files_to_touch, risks)",
                )
            )
        else:
            self._emit("scope", "running", "analyzing task and selecting files")
            scope_result = invoke_skill(
                scope_skill,
                SkillInvocation(
                    skill_name="scope",
                    inputs={
                        "title": task.title,
                        "objective": task.objective,
                        "strategy": task.strategy,
                        "allowed_paths": _task_scope.get("allowed_paths") or [],
                        "forbidden_paths": _task_scope.get("forbidden_paths") or [],
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
            if scope_result.success:
                artifacts.append(
                    _write_artifact(
                        run_dir, "plan",
                        scope_result.output,
                        "Scope-derived plan (approach, files_to_touch, risks)",
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
                    "failure_category": "scope_too_broad",
                    "failure_message": scope.get("approach", "Scope marked too_large"),
                    "needs_split": True,
                    "validation_profile": task.validation_profile,
                },
            )

        self._emit("scope", "done", f"{len(scope.get('files_to_touch', []))} files, {scope.get('estimated_complexity', '?')} complexity")
        self._emit_stage_span("scope", _scope_start, task_id=task.id, run_id=run.id, success=True)
        # STAGE 2: /implement
        _implement_start = time.perf_counter()
        self._emit("implement", "running", "writing code edits")
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
                    "failure_category": "scope_violation",
                    "failure_message": f"Rejected: {apply_summary['rejected']}",
                    "validation_profile": task.validation_profile,
                },
            )

        self._emit("implement", "done", f"{apply_summary.get('edits_applied', 0)} edits, {apply_summary.get('new_files_created', 0)} new files")
        self._emit_stage_span("implement", _implement_start, task_id=task.id, run_id=run.id, success=True)
        # STAGE 3: /self-review
        _self_review_start = time.perf_counter()
        self._emit("self_review", "running", "staff engineer reviewing diff")
        diff_text = _git_diff(workspace)
        non_negotiables = list((task.scope or {}).get("non_negotiables") or [])
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
                    "non_negotiables": non_negotiables,
                },
                task=task,
                run=run,
                run_dir=run_dir / "skill_self_review",
            ),
            self.llm_router,
            telemetry=self.telemetry,
        )
        # Deterministic non-negotiable enforcement runs AFTER the LLM self-review
        # and overrides its verdict if a forbidden file is touched or a required
        # file is missing from the diff. The LLM can't argue out of this check.
        if self_review_result.success and non_negotiables:
            enforced = self_review_skill.enforce_non_negotiables(
                self_review_result.output, non_negotiables, diff_text,
            )
            self_review_result.output.update(enforced)
        artifacts.append(
            _write_artifact(
                run_dir, "self_review_output",
                {"success": self_review_result.success, "errors": self_review_result.errors, "output": self_review_result.output},
                "Staff-engineer self-review",
            )
        )
        ship_ready = bool(self_review_result.output.get("ship_ready")) if self_review_result.success else False

        self._emit("self_review", "done", "ship_ready" if ship_ready else "NOT ship_ready")
        self._emit_stage_span("self_review", _self_review_start, task_id=task.id, run_id=run.id, ship_ready=ship_ready)
        # STAGE 3.5 (conditional): implement+self_review retry loop.
        # When self-review blocks shipping, feed its findings back into
        # /implement as retry_feedback and retry up to _IMPL_SELF_REVIEW_MAX_ROUNDS
        # total rounds. Closes the gap where work_orchestrator had no meta
        # red-team loop — previously a single not-ship_ready verdict from
        # self_review aborted the whole task without the engineer ever
        # seeing the critique.
        _IMPL_SELF_REVIEW_MAX_ROUNDS = 2
        _impl_sr_round = 1
        while (
            not ship_ready
            and _impl_sr_round < _IMPL_SELF_REVIEW_MAX_ROUNDS
            and self_review_result.success
            and implement_result.success
        ):
            _impl_sr_round += 1
            prior_feedback = SelfReviewSkill.feedback_for_retry(self_review_result)
            combined_retry_feedback = "\n\n".join(
                s for s in (retry_feedback, prior_feedback) if s
            )
            # Reload file contents — round 1's apply_changes mutated the
            # workspace, so the LLM needs the post-round-1 state as ground
            # truth for its edit anchors.
            file_contents = _load_file_contents(workspace, files_to_touch)
            reference_contents = _load_reference_contents(
                workspace, files_to_touch, file_contents,
            )
            self._emit(
                "implement", "running",
                f"retry round {_impl_sr_round} after self-review feedback",
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
                        "retry_feedback": combined_retry_feedback,
                    },
                    task=task,
                    run=run,
                    run_dir=run_dir / f"skill_implement_retry_{_impl_sr_round}",
                ),
                self.llm_router,
                telemetry=self.telemetry,
            )
            artifacts.append(
                _write_artifact(
                    run_dir, f"implementation_output_retry_{_impl_sr_round}",
                    {
                        "success": implement_result.success,
                        "errors": implement_result.errors,
                        "output": implement_result.output,
                    },
                    f"Structured implementation output (retry round {_impl_sr_round})",
                )
            )
            if not implement_result.success:
                break
            retry_apply_summary = apply_changes(
                implement_result,
                workspace_root=workspace,
                allowed_files=list(scope.get("files_to_touch") or []),
            )
            artifacts.append(
                _write_artifact(
                    run_dir, f"apply_changes_summary_retry_{_impl_sr_round}",
                    retry_apply_summary,
                    f"File-apply audit (retry round {_impl_sr_round})",
                )
            )
            # Merge retry writes into apply_summary so downstream validate +
            # changed-file tracking sees the cumulative set.
            for _key in ("written", "rejected"):
                _combined = list(apply_summary.get(_key) or []) + list(
                    retry_apply_summary.get(_key) or []
                )
                apply_summary[_key] = list(dict.fromkeys(_combined))
            apply_summary["edits_applied"] = (
                apply_summary.get("edits_applied") or 0
            ) + (retry_apply_summary.get("edits_applied") or 0)
            apply_summary["new_files_created"] = (
                apply_summary.get("new_files_created") or 0
            ) + (retry_apply_summary.get("new_files_created") or 0)
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
                        "non_negotiables": non_negotiables,
                    },
                    task=task,
                    run=run,
                    run_dir=run_dir / f"skill_self_review_retry_{_impl_sr_round}",
                ),
                self.llm_router,
                telemetry=self.telemetry,
            )
            if self_review_result.success and non_negotiables:
                enforced = self_review_skill.enforce_non_negotiables(
                    self_review_result.output, non_negotiables, diff_text,
                )
                self_review_result.output.update(enforced)
            artifacts.append(
                _write_artifact(
                    run_dir, f"self_review_output_retry_{_impl_sr_round}",
                    {
                        "success": self_review_result.success,
                        "errors": self_review_result.errors,
                        "output": self_review_result.output,
                    },
                    f"Staff-engineer self-review (retry round {_impl_sr_round})",
                )
            )
            ship_ready = (
                bool(self_review_result.output.get("ship_ready"))
                if self_review_result.success
                else False
            )
            self._emit(
                "self_review", "done",
                f"retry {_impl_sr_round}: "
                + ("ship_ready" if ship_ready else "still NOT ship_ready"),
            )
        # STAGE 4: /validate (deterministic)
        _validate_start = time.perf_counter()
        self._emit("validate", "running", "compile + tests")
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

        self._emit("validate", "done" if overall != "fail" else "failed", overall)
        self._emit_stage_span("validate", _validate_start, task_id=task.id, run_id=run.id, overall=overall)
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

        # STAGE 6: /quality-gate (deterministic, runs on every successful pipeline)
        _quality_gate_start = time.perf_counter()
        self._emit("quality_gate", "running", "lint, security, docs, types")
        quality_concerns: list[dict[str, str]] = []
        if overall != "fail":
            from ..skills.quality_gate import QualityGateSkill

            qg = QualityGateSkill()
            qg_result = qg.invoke_deterministic(
                workspace=workspace,
                changed_files=list(apply_summary.get("written") or []),
                run_dir=run_dir / "skill_quality_gate",
                validation_profile=profile,
            )
            artifacts.append(
                _write_artifact(
                    run_dir, "quality_gate_output", qg_result.output,
                    "Automatic quality enforcement (lint, security, docs, types)",
                )
            )
            quality_concerns = list(qg_result.output.get("quality_concerns") or [])

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
                    "quality_concerns": quality_concerns,
                    "diagnosis": diagnosis,
                },
                "Skills-pipeline consolidated report",
            )
        )
        if overall == "fail":
            fail_diagnostics: dict[str, Any] = {
                "stage": "validate",
                "worker_outcome": "failed",
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
                    "failure_category": "self_review_blocked",
                    "failure_message": review_feedback[:500],
                    "review_feedback": review_feedback,
                    "validation_profile": profile,
                    "changed_files": written,
                },
            )
        self._emit("quality_gate", "done", qg_result.output.get("summary", "") if overall != "fail" else "skipped")
        self._emit_stage_span("quality_gate", _quality_gate_start, task_id=task.id, run_id=run.id)
        # STAGE 6.5: Auto-append CHANGELOG entry before committing
        rationale = implement_result.output.get("rationale") or ""
        _append_changelog_entry(
            workspace=workspace,
            task_title=task.title,
            rationale=rationale,
            changed_files=written,
            quality_summary=str(
                next((c.get("summary", "") for c in quality_concerns), "")
            ) if quality_concerns else "",
        )
        # Include CHANGELOG.md in the commit if it was updated
        changelog_path = "CHANGELOG.md"
        if (workspace / changelog_path).exists():
            written_with_changelog = list(written) + (
                [changelog_path] if changelog_path not in written else []
            )
        else:
            written_with_changelog = list(written)

        # STAGE 7: /commit — persist changes in git
        _commit_start = time.perf_counter()
        self._emit("commit", "running", "staging + committing")
        deleted_files = implement_result.output.get("deleted_files") or []
        commit_paths = written_with_changelog + [p for p in deleted_files if p not in written_with_changelog]
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
        self._emit_stage_span(
            "commit", _commit_start, task_id=task.id, run_id=run.id,
            success=commit_result.success,
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
    ws_abs = workspace.resolve()
    for content in file_contents.values():
        for module in _parse_imports(content):
            path = _module_to_path(module, workspace)
            if path is not None:
                resolved = path.resolve()
                try:
                    rel = resolved.relative_to(ws_abs).as_posix()
                except ValueError:
                    continue
                if rel not in seen:
                    seen.add(rel)
                    candidates.append(resolved)

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
    ws_resolved = workspace.resolve()
    for path in candidates[:max_ref_files]:
        resolved = path.resolve() if not path.is_absolute() else path
        if not resolved.exists() or not resolved.is_file():
            continue
        try:
            rel = resolved.relative_to(ws_resolved).as_posix()
        except ValueError:
            continue  # outside workspace
        try:
            text = resolved.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        budget = min(max_bytes_per_file, max_total_bytes - total)
        if budget <= 0:
            break
        text = text[:budget]
        total += len(text)
        result[rel] = text
    return result


def _append_changelog_entry(
    *,
    workspace: Path,
    task_title: str,
    rationale: str,
    changed_files: list[str],
    quality_summary: str = "",
) -> None:
    """Append a human-readable entry to CHANGELOG.md in the workspace.

    Creates the file if it doesn't exist. Each entry has: date, title,
    what changed (plain language), files affected.
    """
    from datetime import date

    changelog = workspace / "CHANGELOG.md"
    try:
        existing = changelog.read_text(encoding="utf-8") if changelog.exists() else ""
    except OSError:
        existing = ""
    if not existing.strip():
        existing = "# Changelog\n\nAll notable changes to this project.\n\n"
    entry_lines = [
        f"## {date.today().isoformat()} — {task_title}",
        "",
        rationale.strip() if rationale.strip() else "No description provided.",
        "",
        f"**Files changed:** {', '.join(changed_files) if changed_files else 'none'}",
    ]
    if quality_summary:
        entry_lines.append(f"**Quality notes:** {quality_summary}")
    entry_lines.append("")
    entry = "\n".join(entry_lines)
    # Insert after header (first blank line after the title)
    header_end = existing.find("\n\n")
    if header_end > 0:
        updated = existing[: header_end + 2] + entry + "\n" + existing[header_end + 2:]
    else:
        updated = existing + "\n" + entry
    try:
        changelog.write_text(updated, encoding="utf-8")
    except OSError:
        pass  # Non-fatal: changelog is a convenience, not a gate


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
    # Skip validation when the workspace lacks the expected build tooling.
    # This happens in ephemeral workspaces (smoke tests, generic adapters)
    # where there is no project infrastructure to validate against.
    if workspace is not None:
        _PROFILE_MARKERS = {
            "generic": "Makefile",
            "javascript": "package.json",
        }
        marker = _PROFILE_MARKERS.get(profile)
        if marker and not (workspace / marker).exists():
            return []
    if profile != "python":
        return default_commands
    # If no Python files were changed, skip Python-specific validation.
    py_changed = [p for p in changed_files if p.endswith(".py")]
    if not py_changed:
        return []
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
