"""HarnessUIDataService promotion methods."""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

from ..domain import (
    ContextRecord, Objective, PromotionMode, PromotionStatus,
    Run, RunStatus, Task, TaskStatus, new_id,
)

class PromotionMixin:

    def promote_objective_to_repo(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        project = self.store.get_project(objective.project_id)
        if project is None:
            raise ValueError(f"Unknown project for objective: {objective.project_id}")
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
        review = self._promotion_review_for_objective(objective.id, linked_tasks)
        override_active = bool(review.get("operator_override"))
        if not bool(review.get("review_clear")) and not override_active:
            raise ValueError("Objective is not yet clear to promote")
        candidate_tasks = self._completed_unapplied_tasks_for_objective(linked_tasks)
        if not candidate_tasks:
            raise ValueError("No unapplied completed atomic units are available for repo promotion")
        return self._apply_repo_promotion_for_tasks(objective, project, linked_tasks, candidate_tasks)


    def promote_atomic_unit_to_repo(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        objective_id = str(task.objective_id or "").strip()
        if not objective_id:
            raise ValueError("Atomic-unit repo promotion requires a task linked to an objective")
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective for task: {objective_id}")
        project = self.store.get_project(objective.project_id)
        if project is None:
            raise ValueError(f"Unknown project for objective: {objective.project_id}")
        linked_tasks = [candidate for candidate in self.store.list_tasks(objective.project_id) if candidate.objective_id == objective.id]
        if task.status != TaskStatus.COMPLETED:
            raise ValueError("Only completed atomic units can be promoted to the repository")
        if self._task_repo_applied(task):
            raise ValueError("This atomic unit has already been promoted to the repository")
        return self._apply_repo_promotion_for_tasks(objective, project, linked_tasks, [task])


    def _apply_repo_promotion_for_tasks(
        self,
        objective: Objective,
        project: Project,
        linked_tasks: list[Task],
        candidate_tasks: list[Task],
    ) -> dict[str, object]:
        review = self._promotion_review_for_objective(objective.id, linked_tasks)
        override_active = bool(review.get("operator_override"))
        if not bool(review.get("review_clear")) and not override_active:
            raise ValueError("Objective is not yet clear to promote")
        blocker_reason = self._unapplied_repo_promotion_blocker(candidate_tasks)
        if blocker_reason:
            raise ValueError(blocker_reason)
        source_repo_root = self._objective_source_repo_root(objective.id, linked_tasks)
        if source_repo_root is None:
            raise ValueError("Objective promotion requires a git-backed source repository root")
        objective_paths = self._objective_repo_file_set(candidate_tasks)
        if not objective_paths:
            raise ValueError("Objective promotion could not determine any objective-related file paths to apply")
        candidate = candidate_tasks[-1]
        candidate_run = self._latest_completed_run(candidate)
        candidate_run_id = candidate_run.id if candidate_run is not None else ""
        apply_result = self.ctx.engine.repository_promotions.apply_objective(
            project,
            objective_id=objective.id,
            objective_title=objective.title,
            source_repo_root=source_repo_root,
            source_working_root=source_repo_root,
            objective_paths=objective_paths,
            staging_root=self.workspace_root / "objective_promotions",
        )
        applyback = {
            "status": "applied",
            "branch_name": apply_result.branch_name,
            "commit_sha": apply_result.commit_sha,
            "pushed_ref": apply_result.pushed_ref,
            "pr_url": apply_result.pr_url,
            "promotion_mode": project.promotion_mode.value,
            "cleanup_performed": apply_result.cleanup_performed,
            "verified_remote_sha": apply_result.verified_remote_sha,
            "objective_paths": objective_paths,
            "applied_task_ids": [task.id for task in candidate_tasks],
            "applied_task_count": len(candidate_tasks),
            "source_repo_root": str(source_repo_root),
        }
        applied_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        for task in candidate_tasks:
            metadata = dict(task.external_ref_metadata) if isinstance(task.external_ref_metadata, dict) else {}
            task_run = self._latest_completed_run(task)
            metadata["repo_applyback"] = {
                "applied_commit_sha": apply_result.commit_sha,
                "applied_at": applied_at,
                "pushed_ref": apply_result.pushed_ref,
                "objective_id": objective.id,
                "run_id": task_run.id if task_run is not None else "",
            }
            self.store.update_task_external_metadata(task.id, metadata)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                task_id=candidate.id if candidate is not None else None,
                run_id=candidate_run_id or None,
                visibility="operator_visible",
                author_type="system",
                content="Action receipt: Promoted the objective snapshot to the repository.",
                metadata={
                    "kind": "objective_repo_promotion",
                    "task_id": candidate.id if candidate is not None else "",
                    "run_id": candidate_run_id,
                    "promotion_status": "approved",
                    "applyback": applyback,
                    "objective_paths": objective_paths,
                    "applied_task_ids": [task.id for task in candidate_tasks],
                },
            )
        )
        return {
            "objective_id": objective.id,
            "task_id": candidate.id if candidate is not None else "",
            "run_id": candidate_run_id,
            "promotion": {
                "id": new_id("promotion"),
                "task_id": candidate.id if candidate is not None else "",
                "run_id": candidate_run_id,
                "status": "approved",
                "summary": "Objective snapshot promoted to the repository.",
                "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
            },
            "applyback": applyback,
        }


    def _latest_completed_task_for_objective(self, linked_tasks: list[Task]) -> Task | None:
        best: tuple[str, str, str] | None = None
        selected: Task | None = None
        for task in linked_tasks:
            if task.status != TaskStatus.COMPLETED:
                continue
            runs = self.store.list_runs(task.id)
            completed_run = next((run for run in reversed(runs) if run.status == RunStatus.COMPLETED), None)
            if completed_run is None:
                continue
            score = (
                str(completed_run.created_at or ""),
                str(task.created_at or ""),
                task.id,
            )
            if best is None or score > best:
                best = score
                selected = task
        return selected


    def _objective_repo_file_set(self, linked_tasks: list[Task]) -> list[str]:
        file_paths: set[str] = set()
        for task in linked_tasks:
            runs = self.store.list_runs(task.id)
            for run in runs:
                report_artifacts = [artifact for artifact in self.store.list_artifacts(run.id) if artifact.kind == "report" and artifact.path]
                if not report_artifacts:
                    continue
                report_path = Path(report_artifacts[-1].path)
                try:
                    payload = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                changed_files = payload.get("changed_files")
                if isinstance(changed_files, list):
                    for raw_path in changed_files:
                        path = str(raw_path or "").strip()
                        if path and not path.startswith("/") and ".." not in Path(path).parts:
                            file_paths.add(str(Path(path)))
        return sorted(file_paths)


    def _latest_completed_run(self, task: Task) -> Run | None:
        runs = self.store.list_runs(task.id)
        return next((run for run in reversed(runs) if run.status == RunStatus.COMPLETED), None)


    def _task_repo_applied(self, task: Task) -> bool:
        metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
        repo_applyback = metadata.get("repo_applyback") if isinstance(metadata.get("repo_applyback"), dict) else {}
        return bool(str(repo_applyback.get("applied_commit_sha") or "").strip())


    def _completed_unapplied_tasks_for_objective(self, linked_tasks: list[Task]) -> list[Task]:
        ordered: list[tuple[tuple[str, str, str], Task]] = []
        for task in linked_tasks:
            if task.status != TaskStatus.COMPLETED:
                continue
            if self._task_repo_applied(task):
                continue
            completed_run = self._latest_completed_run(task)
            if completed_run is None:
                continue
            ordered.append(
                (
                    (
                        str(completed_run.created_at or ""),
                        str(task.created_at or ""),
                        task.id,
                    ),
                    task,
                )
            )
        ordered.sort(key=lambda item: item[0])
        return [task for _, task in ordered]


    def _unapplied_repo_promotion_blocker(self, tasks: list[Task]) -> str:
        for task in tasks:
            completed_run = self._latest_completed_run(task)
            if completed_run is None:
                return f"Completed atomic unit '{task.title}' does not have a completed run."
            missing_validation_reason = self._missing_repo_promotion_validation_reason(completed_run.id)
            if missing_validation_reason:
                return f"Completed atomic unit '{task.title}' is not ready for repo promotion. {missing_validation_reason}"
        return ""


    def _objective_source_repo_root(self, objective_id: str, linked_tasks: list[Task]) -> Path | None:
        for task in reversed(linked_tasks):
            runs = self.store.list_runs(task.id)
            for run in reversed(runs):
                events = self.store.list_events(entity_type="run", entity_id=run.id)
                for event in reversed(events):
                    if event.event_type != "project_workspace_prepared":
                        continue
                    source_repo_root = str(event.payload.get("source_repo_root") or "").strip()
                    if source_repo_root:
                        return Path(source_repo_root).resolve()
        return None


    def _latest_objective_repo_promotion(self, objective_id: str) -> dict[str, object] | None:
        records = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="action_receipt")
            if str(record.metadata.get("kind") or "") == "objective_repo_promotion"
        ]
        if not records:
            return None
        record = records[-1]
        applyback = dict(record.metadata.get("applyback") or {})
        return {
            "id": record.id,
            "status": "approved",
            "summary": record.content,
            "created_at": record.created_at.isoformat(),
            "applyback": applyback,
            "task_id": str(record.metadata.get("task_id") or ""),
            "run_id": str(record.metadata.get("run_id") or ""),
        }


    def _missing_repo_promotion_validation_reason(self, run_id: str) -> str:
        report_artifacts = [artifact for artifact in self.store.list_artifacts(run_id) if artifact.kind == "report" and artifact.path]
        if not report_artifacts:
            return "The latest completed run does not have a structured report artifact."
        report_path = Path(report_artifacts[-1].path)
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "The latest completed run has an unreadable structured report artifact."
        compile_check = payload.get("compile_check")
        test_check = payload.get("test_check")
        if isinstance(compile_check, dict) and isinstance(test_check, dict):
            return ""
        return (
            "The latest completed run is missing persisted compile/test validation evidence in report.json. "
            "Re-run or re-validate the task before repo promotion."
        )


    def _repo_promotion_for_objective(self, objective_id: str, linked_tasks: list[Task]) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        project = self.store.get_project(objective.project_id)
        if project is None:
            raise ValueError(f"Unknown project for objective: {objective.project_id}")
        review = self._promotion_review_for_objective(objective.id, linked_tasks)
        override_active = bool(review.get("operator_override"))
        candidate = self._latest_completed_task_for_objective(linked_tasks)
        candidate_payload: dict[str, object] | None = None
        latest_promotion_payload: dict[str, object] | None = self._latest_objective_repo_promotion(objective.id)
        reason = ""
        eligible = False
        source_repo_root = self._objective_source_repo_root(objective.id, linked_tasks)
        unapplied_completed_tasks = self._completed_unapplied_tasks_for_objective(linked_tasks)
        objective_paths = self._objective_repo_file_set(unapplied_completed_tasks)

        if not unapplied_completed_tasks:
            if any(task.status == TaskStatus.COMPLETED for task in linked_tasks):
                reason = "All completed atomic units for this objective have already been promoted."
            else:
                reason = "No completed linked task is available yet."
        else:
            candidate = unapplied_completed_tasks[-1]
            completed_run = self._latest_completed_run(candidate)
            blocker_reason = self._unapplied_repo_promotion_blocker(unapplied_completed_tasks)
            candidate_payload = {
                "task_id": candidate.id,
                "title": candidate.title,
                "status": candidate.status.value,
                "latest_completed_run_id": completed_run.id if completed_run is not None else "",
                "latest_completed_attempt": completed_run.attempt if completed_run is not None else None,
                "unapplied_completed_task_count": len(unapplied_completed_tasks),
                "unapplied_completed_task_ids": [task.id for task in unapplied_completed_tasks],
            }
            if blocker_reason:
                reason = blocker_reason
            elif not objective_paths:
                reason = "Objective promotion could not determine any objective-related file paths to apply."
            elif source_repo_root is None:
                reason = "Objective promotion requires a git-backed source repository root."
            else:
                if not bool(review.get("review_clear")) and not override_active:
                    reason = "Objective review must be clear before repo promotion."
                else:
                    eligible = True
                    reason = (
                        f"Operator override is active. Repo promotion will batch {len(unapplied_completed_tasks)} unapplied completed atomic unit(s) across {len(objective_paths)} tracked file(s) and apply them to the repository."
                        if override_active and not bool(review.get("review_clear"))
                        else f"{len(unapplied_completed_tasks)} unapplied completed atomic unit(s) are ready to promote to the repository across {len(objective_paths)} tracked file(s)."
                    )

        return {
            "eligible": eligible,
            "reason": reason,
            "project_settings": {
                "promotion_mode": project.promotion_mode.value,
                "repo_provider": project.repo_provider.value if project.repo_provider is not None else "",
                "repo_name": project.repo_name,
                "base_branch": project.base_branch,
            },
            "candidate": candidate_payload,
            "latest_promotion": latest_promotion_payload,
        }

