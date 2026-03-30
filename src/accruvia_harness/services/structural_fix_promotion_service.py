from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..control_breadcrumbs import BreadcrumbWriter
from ..domain import ControlRecoveryAction, Project, Task, new_id
from ..store import SQLiteHarnessStore
from .repository_promotion_service import LocalCIResult, RepositoryPromotionService


@dataclass(slots=True)
class StructuralFixPromotionResult:
    task_id: str
    objective_id: str | None
    ci_started_at: datetime
    ci_finished_at: datetime
    ci_passed: bool
    failed_stage: str
    summary: str
    commit_sha: str | None = None
    push_status: str = "not_attempted"
    workspace_root: str | None = None
    logs: dict[str, str] | None = None


class StructuralFixPromotionService:
    def __init__(
        self,
        store: SQLiteHarnessStore,
        breadcrumb_writer: BreadcrumbWriter,
        repository_promotions: RepositoryPromotionService,
    ) -> None:
        self.store = store
        self.breadcrumb_writer = breadcrumb_writer
        self.repository_promotions = repository_promotions

    def promote_completed_structural_fix(self, task: Task, run_id: str) -> dict[str, object]:
        if str(task.strategy or "") != "sa_structural_fix":
            return {"status": "skipped", "reason": "not_structural_fix", "task_id": task.id}
        workspace_details = self._workspace_details_for_run(run_id)
        now = datetime.now(UTC)
        if workspace_details is None:
            result = StructuralFixPromotionResult(
                task_id=task.id,
                objective_id=task.objective_id,
                ci_started_at=now,
                ci_finished_at=now,
                ci_passed=False,
                failed_stage="unknown",
                summary="Structural fix promotion skipped because workspace details are missing.",
            )
            self._record(task, run_id, result)
            return {"status": "skipped", "reason": "missing_workspace_details", **self._serialize(result)}

        workspace_root = Path(str(workspace_details.get("project_root") or "")).resolve()
        if not workspace_root.exists():
            result = StructuralFixPromotionResult(
                task_id=task.id,
                objective_id=task.objective_id,
                ci_started_at=now,
                ci_finished_at=now,
                ci_passed=False,
                failed_stage="unknown",
                summary="Structural fix promotion skipped because the prepared workspace is missing.",
                workspace_root=str(workspace_root),
            )
            self._record(task, run_id, result)
            return {"status": "skipped", "reason": "workspace_missing", **self._serialize(result)}

        ci_result = self.repository_promotions.run_local_ci(workspace_root)
        result = StructuralFixPromotionResult(
            task_id=task.id,
            objective_id=task.objective_id,
            ci_started_at=ci_result.started_at,
            ci_finished_at=ci_result.finished_at,
            ci_passed=ci_result.passed,
            failed_stage=ci_result.failed_stage,
            summary=ci_result.summary,
            workspace_root=str(workspace_root),
            logs=dict(ci_result.logs),
        )
        if not ci_result.passed:
            self._record_failure_action(task, ci_result, reason="structural_fix_ci_failed")
            self._record(task, run_id, result)
            return {"status": "blocked", "reason": "ci_failed", **self._serialize(result)}

        try:
            project = self.store.get_project(task.project_id)
            if project is None:
                raise RuntimeError(f"Unknown project for task {task.id}")
            result.commit_sha = self._commit_and_push(project, task, run_id, workspace_root, workspace_details)
            result.push_status = "pushed"
            result.summary = "Local CI parity passed and the structural fix was pushed to main."
            self._record(task, run_id, result)
            return {"status": "pushed", **self._serialize(result)}
        except Exception as exc:
            result.push_status = f"failed: {exc}"
            result.summary = f"Local CI parity passed, but promotion to main failed: {exc}"
            self._record_failure_action(task, ci_result, reason="structural_fix_promotion_failed")
            self._record(task, run_id, result)
            return {"status": "blocked", "reason": "push_failed", **self._serialize(result)}

    def _commit_and_push(
        self,
        project: Project,
        task: Task,
        run_id: str,
        workspace_root: Path,
        workspace_details: dict[str, object],
    ) -> str:
        workspace_mode = str(workspace_details.get("workspace_mode") or "")
        changed_paths = self._changed_paths_for_run(run_id)
        if workspace_mode == "shared_repo":
            self._assert_no_unrelated_shared_repo_changes(workspace_root, changed_paths)
        self._stage_repair_changes(
            workspace_root,
            changed_paths,
            isolated=workspace_mode in {"git_worktree", "git_clone"},
        )
        staged = self.repository_promotions._git_output(workspace_root, "diff", "--cached", "--name-only").strip()
        if not staged:
            raise RuntimeError("No repair changes were available to commit.")
        self.repository_promotions._git(
            workspace_root,
            "commit",
            "-m",
            f"sa-watch: unblock objective {task.objective_id or task.id}",
        )
        commit_sha = self.repository_promotions._git_output(workspace_root, "rev-parse", "HEAD").strip()
        self.repository_promotions._git(workspace_root, "push", "origin", "HEAD:main")
        self.repository_promotions._verify_remote_sha(workspace_root, "main", commit_sha)
        return commit_sha

    def _stage_repair_changes(self, workspace_root: Path, changed_paths: list[str], *, isolated: bool) -> None:
        if changed_paths:
            subprocess.run(
                ["git", "add", "-A", "--", *changed_paths],
                cwd=workspace_root,
                check=True,
                capture_output=True,
                text=True,
            )
            return
        if isolated:
            self.repository_promotions._git(workspace_root, "add", "-A")
            return
        raise RuntimeError("Unable to determine which repair files to stage in the shared repository.")

    def _assert_no_unrelated_shared_repo_changes(self, workspace_root: Path, changed_paths: list[str]) -> None:
        if not changed_paths:
            raise RuntimeError("Shared-repo promotion needs explicit changed_files evidence before staging.")
        allowed = set(changed_paths)
        status_output = self.repository_promotions._git_output(workspace_root, "status", "--porcelain")
        unexpected: list[str] = []
        for raw_line in status_output.splitlines():
            candidate = raw_line[3:].strip()
            if not candidate:
                continue
            if candidate.startswith('"') and candidate.endswith('"'):
                candidate = candidate[1:-1]
            if " -> " in candidate:
                candidate = candidate.split(" -> ", 1)[1].strip()
            if candidate not in allowed:
                unexpected.append(candidate)
        if unexpected:
            raise RuntimeError(
                "Shared-repo promotion blocked because unrelated local changes are present: "
                + ", ".join(sorted(set(unexpected)))
            )

    def _changed_paths_for_run(self, run_id: str) -> list[str]:
        run_dir = self.store.db_path.parent / "workspace" / "runs" / run_id
        report_path = run_dir / "report.json"
        if not report_path.exists():
            return []
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        changed_files = payload.get("changed_files")
        if not isinstance(changed_files, list):
            return []
        cleaned: list[str] = []
        for item in changed_files:
            value = str(item or "").strip().replace("\\", "/")
            if not value or value.startswith("/") or value.startswith("../") or "/../" in f"/{value}":
                continue
            cleaned.append(str(Path(value)))
        return sorted(dict.fromkeys(cleaned))

    def _workspace_details_for_run(self, run_id: str) -> dict[str, object] | None:
        events = self.store.list_events(entity_type="run", entity_id=run_id)
        for event in reversed(events):
            if event.event_type == "project_workspace_prepared":
                return dict(event.payload)
        return None

    def _record_failure_action(self, task: Task, ci_result: LocalCIResult, *, reason: str) -> None:
        self.store.create_control_recovery_action(
            ControlRecoveryAction(
                id=new_id("recovery"),
                action_type="observe",
                target_type="task",
                target_id=task.id,
                reason=reason,
                result=ci_result.failed_stage,
            )
        )

    def _record(self, task: Task, run_id: str, result: StructuralFixPromotionResult) -> None:
        payload = self._serialize(result)
        metadata = dict(task.external_ref_metadata or {})
        metadata["sa_watch_promotion"] = payload
        self.store.update_task_external_metadata(task.id, metadata)
        classification = "ci_passed" if result.ci_passed and result.push_status == "pushed" else "ci_failed"
        self.breadcrumb_writer.write_bundle(
            entity_type="task",
            entity_id=task.id,
            meta={
                "task_id": task.id,
                "objective_id": task.objective_id,
                "run_id": run_id,
                "ci_started_at": payload["ci_started_at"],
                "ci_finished_at": payload["ci_finished_at"],
                "commit_sha": result.commit_sha,
                "push_status": result.push_status,
            },
            evidence={
                "ci_passed": result.ci_passed,
                "failed_stage": result.failed_stage,
                "summary": result.summary,
                "logs": result.logs or {},
                "workspace_root": result.workspace_root,
            },
            decision={
                "classification": classification,
                "ci_result": {
                    "passed": result.ci_passed,
                    "failed_stage": result.failed_stage,
                    "summary": result.summary,
                },
                "commit_sha": result.commit_sha,
                "push_status": result.push_status,
            },
            worker_run_id=run_id,
            classification=classification,
            summary=result.summary,
        )

    @staticmethod
    def _serialize(result: StructuralFixPromotionResult) -> dict[str, object]:
        payload = asdict(result)
        payload["ci_started_at"] = result.ci_started_at.isoformat()
        payload["ci_finished_at"] = result.ci_finished_at.isoformat()
        return payload
