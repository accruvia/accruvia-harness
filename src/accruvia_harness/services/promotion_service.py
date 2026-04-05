from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from ..domain import Event, PromotionRecord, PromotionStatus, RunStatus, new_id
from ..llm import LLMInvocation, LLMRouter, parse_affirmation_response
from ..skills import PromotionReviewSkill, SkillInvocation, invoke_skill
from ..store import SQLiteHarnessStore
from ..validation import PromotionValidator, PromotionValidatorRegistry, ValidationIssue, default_promotion_validators
from .repository_promotion_service import RepositoryPromotionService
from .task_service import TaskService


@dataclass(slots=True)
class PromotionReviewResult:
    promotion: PromotionRecord
    follow_on_task_id: str | None


class PromotionService:
    def __init__(
        self,
        store: SQLiteHarnessStore,
        task_service: TaskService,
        workspace_root: Path,
        validators: list[PromotionValidator] | None = None,
        validator_registry: PromotionValidatorRegistry | None = None,
        llm_router: LLMRouter | None = None,
        telemetry=None,
        repository_promotions: RepositoryPromotionService | None = None,
    ) -> None:
        self.store = store
        self.task_service = task_service
        self.workspace_root = workspace_root
        self.validators = validators
        self.validator_registry = validator_registry
        self.llm_router = llm_router
        self.telemetry = telemetry
        self.repository_promotions = repository_promotions or RepositoryPromotionService()

    def review_task(self, task_id: str, run_id: str | None = None, create_follow_on: bool = True) -> PromotionReviewResult:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        validators = self.validators or (
            self.validator_registry.validators_for_profile(task.validation_profile)
            if self.validator_registry is not None
            else default_promotion_validators(task.validation_profile)
        )
        run = self._select_run(task_id, run_id)
        if run.status != RunStatus.COMPLETED:
            raise ValueError(f"Run {run.id} is not promotion-eligible")
        artifacts = self.store.list_artifacts(run.id)
        if self.telemetry is not None:
            with self.telemetry.timed(
                "promotion_review",
                task_id=task.id,
                run_id=run.id,
                validation_profile=task.validation_profile,
            ):
                results = [validator.validate(task, artifacts) for validator in validators]
        else:
            results = [validator.validate(task, artifacts) for validator in validators]
        issues = [issue for result in results for issue in result.issues]
        if not issues:
            promotion = PromotionRecord(
                id=new_id("promotion"),
                task_id=task.id,
                run_id=run.id,
                status=PromotionStatus.PENDING,
                summary="Deterministic promotion gates passed; awaiting LLM affirmation.",
                details={
                    "validators": [self._serialize_result(result) for result in results],
                    "affirmation_required": True,
                },
            )
            self.store.create_promotion(promotion)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=task.id,
                    event_type="promotion_pending",
                    payload={"promotion_id": promotion.id, "run_id": run.id},
                )
            )
            if self.telemetry is not None:
                self.telemetry.metric(
                    "promotion_pending",
                    1,
                    task_id=task.id,
                    run_id=run.id,
                    validation_profile=task.validation_profile,
                )
            return PromotionReviewResult(promotion=promotion, follow_on_task_id=None)

        follow_on_task_id: str | None = None
        if create_follow_on:
            existing = self.store.find_follow_on_task(task.id, run.id)
            if existing is not None:
                follow_on_task_id = existing.id
            else:
                title, objective = self._follow_on_from_issues(task.title, issues)
                follow_on = self.task_service.create_follow_on_task(
                    parent_task_id=task.id,
                    source_run_id=run.id,
                    title=title,
                    objective=objective,
                )
                follow_on_task_id = follow_on.id

        promotion = PromotionRecord(
            id=new_id("promotion"),
            task_id=task.id,
            run_id=run.id,
            status=PromotionStatus.REJECTED,
            summary="Promotion review rejected the candidate.",
            details={
                "validators": [self._serialize_result(result) for result in results],
                "issue_count": len(issues),
                "follow_on_task_id": follow_on_task_id,
            },
        )
        self.store.create_promotion(promotion)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="promotion_rejected",
                payload={
                    "promotion_id": promotion.id,
                    "run_id": run.id,
                    "follow_on_task_id": follow_on_task_id,
                },
            )
        )
        if self.telemetry is not None:
            self.telemetry.metric(
                "promotion_rejected",
                1,
                task_id=task.id,
                run_id=run.id,
                validation_profile=task.validation_profile,
                review_mode="review",
            )
        return PromotionReviewResult(promotion=promotion, follow_on_task_id=follow_on_task_id)

    def affirm_review(
        self,
        task_id: str,
        run_id: str | None = None,
        promotion_id: str | None = None,
        create_follow_on: bool = True,
    ) -> PromotionReviewResult:
        if self.llm_router is None:
            raise ValueError("No LLM router configured for promotion affirmation")
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        promotion = self._select_promotion(task_id, run_id=run_id, promotion_id=promotion_id)
        if promotion.status != PromotionStatus.PENDING:
            raise ValueError(f"Promotion {promotion.id} is not pending affirmation")
        run = self._select_run(task_id, promotion.run_id)
        artifacts = self.store.list_artifacts(run.id)
        # Structured promotion review via /promotion-review skill — replaces
        # the old free-form prompt + heuristic affirmation parser.
        review_skill = PromotionReviewSkill()
        skill_inputs = self._build_review_inputs(task, promotion, artifacts)
        skill_result = invoke_skill(
            review_skill,
            SkillInvocation(
                skill_name=review_skill.name,
                inputs=skill_inputs,
                task=task,
                run=run,
                run_dir=self._affirmation_run_dir(run.id),
            ),
            self.llm_router,
            telemetry=self.telemetry,
        )
        if not skill_result.success:
            approved = False
            rationale = f"promotion_review skill failed: {'; '.join(skill_result.errors)}"
            concerns: list[dict[str, str]] = []
        else:
            approved = bool(skill_result.output.get("approved"))
            rationale = str(skill_result.output.get("rationale") or "")
            concerns = list(skill_result.output.get("concerns") or [])
        routed_backend = skill_result.llm_backend or "unknown"
        details = {
            **promotion.details,
            "affirmation": {
                "backend": routed_backend,
                "response_path": skill_result.response_path or "",
                "prompt_path": skill_result.prompt_path or "",
                "rationale": rationale,
                "approved": approved,
                "concerns": concerns,
                "skill": "promotion_review",
            },
        }
        follow_on_task_id: str | None = None
        if approved:
            applyback = self._apply_approved_promotion(task, run)
            promotion = PromotionRecord(
                id=promotion.id,
                task_id=promotion.task_id,
                run_id=promotion.run_id,
                status=PromotionStatus.APPROVED,
                summary="Promotion affirmed by deterministic gates and LLM review.",
                details={**details, "applyback": applyback},
                created_at=promotion.created_at,
            )
            self.store.update_promotion(promotion)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=task.id,
                    event_type="promotion_approved",
                    payload={"promotion_id": promotion.id, "run_id": run.id, "llm_backend": routed_backend},
                )
            )
            if applyback.get("status") == "applied":
                self.store.create_event(
                    Event(
                        id=new_id("event"),
                        entity_type="task",
                        entity_id=task.id,
                        event_type="promotion_applied",
                        payload={"promotion_id": promotion.id, "run_id": run.id, **applyback},
                    )
                )
            if self.telemetry is not None:
                self.telemetry.metric(
                    "promotion_approved",
                    1,
                    task_id=task.id,
                    run_id=run.id,
                    validation_profile=task.validation_profile,
                    llm_backend=routed_backend,
                )
            return PromotionReviewResult(promotion=promotion, follow_on_task_id=None)

        issues = self._issues_from_promotion_details(promotion.details)
        if create_follow_on and issues:
            existing = self.store.find_follow_on_task(task.id, run.id)
            if existing is not None:
                follow_on_task_id = existing.id
            else:
                title, objective = self._follow_on_from_issues(task.title, issues)
                follow_on = self.task_service.create_follow_on_task(
                    parent_task_id=task.id,
                    source_run_id=run.id,
                    title=title,
                    objective=objective,
                )
                follow_on_task_id = follow_on.id
        elif create_follow_on:
            existing = self.store.find_follow_on_task(task.id, run.id)
            if existing is not None:
                follow_on_task_id = existing.id
            else:
                follow_on = self.task_service.create_follow_on_task(
                    parent_task_id=task.id,
                    source_run_id=run.id,
                    title=f"Address LLM promotion concerns for {task.title}",
                    objective="Resolve the LLM promotion concerns recorded in the affirmation rationale and regenerate the candidate.",
                )
                follow_on_task_id = follow_on.id
        details["follow_on_task_id"] = follow_on_task_id
        promotion = PromotionRecord(
            id=promotion.id,
            task_id=promotion.task_id,
            run_id=promotion.run_id,
            status=PromotionStatus.REJECTED,
            summary="Promotion rejected by LLM affirmation.",
            details=details,
            created_at=promotion.created_at,
        )
        self.store.update_promotion(promotion)
        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="task",
                entity_id=task.id,
                event_type="promotion_rejected",
                payload={
                    "promotion_id": promotion.id,
                    "run_id": run.id,
                    "follow_on_task_id": follow_on_task_id,
                    "llm_backend": routed_backend,
                },
            )
        )
        if self.telemetry is not None:
            self.telemetry.metric(
                "promotion_rejected",
                1,
                task_id=task.id,
                run_id=run.id,
                validation_profile=task.validation_profile,
                review_mode="affirmation",
                llm_backend=routed_backend,
            )
        return PromotionReviewResult(promotion=promotion, follow_on_task_id=follow_on_task_id)

    def _apply_approved_promotion(self, task, run) -> dict[str, object]:
        project = self.store.get_project(task.project_id)
        if project is None:
            raise ValueError(f"Unknown project for task: {task.project_id}")
        workspace_details = self._workspace_details_for_run(run.id)
        if workspace_details is None:
            return {"status": "skipped", "reason": "missing_workspace_details"}
        if workspace_details.get("workspace_mode") not in {"git_worktree", "git_clone"}:
            return {
                "status": "skipped",
                "reason": "non_git_workspace",
                "workspace_mode": workspace_details.get("workspace_mode"),
            }
        try:
            remediation = None
            if isinstance(task.external_ref_metadata, dict):
                remediation = task.external_ref_metadata.get("promotion_remediation")
            target_branch = remediation.get("branch_name") if isinstance(remediation, dict) else None
            apply_result = self.repository_promotions.apply(
                project,
                task,
                Path(workspace_details["project_root"]),
                target_branch=target_branch,
                open_review=False if target_branch else None,
            )
        except subprocess.CalledProcessError:
            return {
                "status": "skipped",
                "reason": "non_git_workspace",
                "workspace_mode": workspace_details.get("workspace_mode"),
            }
        return {
            "status": "applied",
            "branch_name": apply_result.branch_name,
            "commit_sha": apply_result.commit_sha,
            "pushed_ref": apply_result.pushed_ref,
            "pr_url": apply_result.pr_url,
            "promotion_mode": project.promotion_mode.value,
            "updated_existing_review": bool(target_branch),
        }

    def _workspace_details_for_run(self, run_id: str) -> dict[str, object] | None:
        events = self.store.list_events(entity_type="run", entity_id=run_id)
        for event in reversed(events):
            if event.event_type == "project_workspace_prepared":
                return dict(event.payload)
        return None

    def rereview_task(
        self,
        task_id: str,
        remediation_task_id: str,
        remediation_run_id: str | None = None,
        base_promotion_id: str | None = None,
        create_follow_on: bool = True,
    ) -> PromotionReviewResult:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        remediation_task = self.store.get_task(remediation_task_id)
        if remediation_task is None:
            raise ValueError(f"Unknown remediation task: {remediation_task_id}")
        base_promotion = self._select_promotion(task_id, promotion_id=base_promotion_id) if base_promotion_id else self.store.latest_promotion(task_id)
        validators = self.validators or (
            self.validator_registry.validators_for_profile(task.validation_profile)
            if self.validator_registry is not None
            else default_promotion_validators(task.validation_profile)
        )
        run = self._select_run(remediation_task_id, remediation_run_id)
        artifacts = self.store.list_artifacts(run.id)
        if self.telemetry is not None:
            with self.telemetry.timed(
                "promotion_rereview",
                task_id=task.id,
                run_id=run.id,
                remediation_task_id=remediation_task_id,
                validation_profile=task.validation_profile,
            ):
                results = [validator.validate(task, artifacts) for validator in validators]
        else:
            results = [validator.validate(task, artifacts) for validator in validators]
        issues = [issue for result in results for issue in result.issues]
        if not issues:
            promotion = PromotionRecord(
                id=new_id("promotion"),
                task_id=task.id,
                run_id=run.id,
                status=PromotionStatus.PENDING,
                summary="Remediation candidate passed deterministic gates; awaiting LLM affirmation.",
                details={
                    "validators": [self._serialize_result(result) for result in results],
                    "affirmation_required": True,
                    "review_mode": "rereview",
                    "base_promotion_id": base_promotion.id if base_promotion else None,
                    "remediation_task_id": remediation_task_id,
                    "remediation_run_id": run.id,
                },
            )
            self.store.create_promotion(promotion)
            if self.telemetry is not None:
                self.telemetry.metric(
                    "promotion_pending",
                    1,
                    task_id=task.id,
                    run_id=run.id,
                    validation_profile=task.validation_profile,
                    review_mode="rereview",
                )
            return PromotionReviewResult(promotion=promotion, follow_on_task_id=None)
        follow_on_task_id: str | None = None
        if create_follow_on:
            existing = self.store.find_follow_on_task(remediation_task.id, run.id)
            if existing is not None:
                follow_on_task_id = existing.id
            else:
                title, objective = self._follow_on_from_issues(task.title, issues)
                follow_on = self.task_service.create_follow_on_task(
                    parent_task_id=remediation_task.id,
                    source_run_id=run.id,
                    title=title,
                    objective=objective,
                )
                follow_on_task_id = follow_on.id
        promotion = PromotionRecord(
            id=new_id("promotion"),
            task_id=task.id,
            run_id=run.id,
            status=PromotionStatus.REJECTED,
            summary="Remediation candidate failed re-review.",
            details={
                "validators": [self._serialize_result(result) for result in results],
                "review_mode": "rereview",
                "base_promotion_id": base_promotion.id if base_promotion else None,
                "remediation_task_id": remediation_task_id,
                "remediation_run_id": run.id,
                "follow_on_task_id": follow_on_task_id,
            },
        )
        self.store.create_promotion(promotion)
        if self.telemetry is not None:
            self.telemetry.metric(
                "promotion_rejected",
                1,
                task_id=task.id,
                run_id=run.id,
                validation_profile=task.validation_profile,
                review_mode="rereview",
            )
        return PromotionReviewResult(promotion=promotion, follow_on_task_id=follow_on_task_id)

    def decompose_review_findings_to_atomic_tasks(
        self,
        *,
        parent_task_id: str,
        source_run_id: str,
        findings: list[dict[str, object]],
    ) -> list[str]:
        created = self.task_service.create_tasks_from_review_findings(
            parent_task_id=parent_task_id,
            source_run_id=source_run_id,
            findings=findings,
        )
        return [task.id for task in created]

    def _select_run(self, task_id: str, run_id: str | None):
        if run_id is not None:
            run = self.store.get_run(run_id)
            if run is None or run.task_id != task_id:
                raise ValueError(f"Unknown run {run_id} for task {task_id}")
            return run
        runs = self.store.list_runs(task_id)
        if not runs:
            raise ValueError(f"Task {task_id} has no runs to review")
        return runs[-1]

    def _serialize_result(self, result) -> dict[str, object]:
        return {
            "validator": result.validator,
            "ok": result.ok,
            "summary": result.summary,
            "issues": [
                {
                    "code": issue.code,
                    "summary": issue.summary,
                    "details": issue.details,
                    "follow_on_title": issue.follow_on_title,
                    "follow_on_objective": issue.follow_on_objective,
                }
                for issue in result.issues
            ],
        }

    def _issues_from_promotion_details(self, details: dict[str, object]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for result in details.get("validators", []):
            if not isinstance(result, dict):
                continue
            for issue in result.get("issues", []):
                if not isinstance(issue, dict):
                    continue
                issues.append(
                    ValidationIssue(
                        code=str(issue.get("code", "unknown_issue")),
                        summary=str(issue.get("summary", "Promotion issue")),
                        details=issue.get("details", {}) if isinstance(issue.get("details"), dict) else {},
                        follow_on_title=issue.get("follow_on_title") if isinstance(issue.get("follow_on_title"), str) else None,
                        follow_on_objective=issue.get("follow_on_objective") if isinstance(issue.get("follow_on_objective"), str) else None,
                    )
                )
        return issues

    def _select_promotion(
        self, task_id: str, run_id: str | None = None, promotion_id: str | None = None
    ) -> PromotionRecord:
        promotions = self.store.list_promotions(task_id)
        if promotion_id is not None:
            for promotion in promotions:
                if promotion.id == promotion_id:
                    return promotion
            raise ValueError(f"Unknown promotion {promotion_id} for task {task_id}")
        if run_id is not None:
            matching = [promotion for promotion in promotions if promotion.run_id == run_id]
            if matching:
                return matching[-1]
            raise ValueError(f"No promotion exists for run {run_id} on task {task_id}")
        if not promotions:
            raise ValueError(f"Task {task_id} has no promotion reviews")
        return promotions[-1]

    def _build_review_inputs(
        self, task, promotion: PromotionRecord, artifacts
    ) -> dict[str, object]:
        """Assemble typed inputs for the /promotion-review skill.

        Diff is approximated from artifact contents when a real git diff is
        not available. The skill is forgiving about this — it accepts any
        'diff' text.
        """
        validator_summaries = promotion.details.get("validators", [])
        changed_files: list[str] = []
        scope_approach = ""
        skill_report_text = ""
        for artifact in artifacts:
            try:
                if artifact.kind == "scope_output":
                    import json as _json
                    payload = _json.loads(Path(artifact.path).read_text(encoding="utf-8"))
                    output = payload.get("output") or {}
                    scope_approach = str(output.get("approach") or "")
                elif artifact.kind == "implementation_output":
                    import json as _json
                    payload = _json.loads(Path(artifact.path).read_text(encoding="utf-8"))
                    output = payload.get("output") or {}
                    for entry in output.get("changed_files") or []:
                        if isinstance(entry, dict) and entry.get("path"):
                            changed_files.append(str(entry["path"]))
                elif artifact.kind == "report":
                    import json as _json
                    payload = _json.loads(Path(artifact.path).read_text(encoding="utf-8"))
                    for item in payload.get("changed_files") or []:
                        changed_files.append(str(item))
            except (OSError, ValueError, KeyError):
                continue
        # Fall back to raw artifact contents as the "diff" context
        skill_report_text = self._artifact_contents(artifacts)
        return {
            "title": task.title,
            "objective": task.objective,
            "diff": skill_report_text,
            "validation_summary": str(validator_summaries),
            "scope_approach": scope_approach,
            "changed_files": list(dict.fromkeys(changed_files)),  # dedupe, preserve order
        }

    def _build_affirmation_prompt(
        self, task, promotion: PromotionRecord, artifacts
    ) -> str:
        validator_summaries = promotion.details.get("validators", [])
        artifact_lines = "\n".join(f"- {artifact.kind}: {artifact.path}" for artifact in artifacts)
        artifact_contents = self._artifact_contents(artifacts)
        return (
            "You are conducting an adversarial audit of a promotion candidate after deterministic validation has already run.\n"
            "Do not assume the validation results are correct or sufficient.\n"
            "Actively look for edge cases, hidden failure modes, weak reasoning, incomplete remediation, and any signs that the candidate should not be promoted.\n"
            "Treat the validator results as inputs to challenge, not proof that the candidate is safe.\n"
            "Use skeptical, critical reasoning to catch problems deterministic checks may have missed.\n"
            "Approve only if the evidence is strong and you cannot find a substantive reason to block promotion.\n"
            "Reply on the first line with APPROVE or REJECT.\n"
            "After the first line, provide a short rationale that focuses on concrete risks, doubts, or the evidence that resolved them.\n\n"
            f"Task: {task.title}\n"
            f"Objective: {task.objective}\n"
            f"Task ID: {task.id}\n"
            f"Run ID: {promotion.run_id}\n"
            f"Validator Results: {validator_summaries}\n"
            f"Artifacts:\n{artifact_lines}\n"
            f"Artifact Contents:\n{artifact_contents}\n"
        )

    def _affirmation_run_dir(self, run_id: str):
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"Unknown run: {run_id}")
        return self.workspace_root / "runs" / run.id / "promotion_affirmation"

    def _follow_on_from_issues(self, task_title: str, issues: list[ValidationIssue]) -> tuple[str, str]:
        if not issues:
            return (
                f"Resolve promotion failure for {task_title}",
                "Address the promotion validation failures recorded for the rejected candidate and regenerate it.",
            )
        title = issues[0].follow_on_title or f"Resolve promotion failure for {task_title}"
        objectives = [issue.follow_on_objective or issue.summary for issue in issues]
        objective_lines = "\n".join(f"- {item}" for item in objectives)
        return (title, f"Address all recorded promotion issues:\n{objective_lines}")

    def _artifact_contents(self, artifacts) -> str:
        sections: list[str] = []
        for artifact in artifacts:
            if artifact.kind not in {"report", "plan"}:
                continue
            path = Path(artifact.path)
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                content = f"<unreadable: {exc}>"
            if len(content) > 4000:
                content = content[:4000] + "\n...<truncated>"
            sections.append(f"## {artifact.kind}: {artifact.path}\n{content}")
        return "\n\n".join(sections) if sections else "<no readable artifact contents>"
