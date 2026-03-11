from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ..domain import Event, PromotionStatus, RepoProvider, new_id
from ..github import GitHubCLI
from ..gitlab import GitLabCLI


@dataclass(slots=True)
class ReviewWatcherResult:
    checked_count: int
    changed_count: int
    conflict_count: int
    merged_count: int
    checked_promotion_ids: list[str]


class ReviewWatcherService:
    def __init__(self, store, task_service=None, github: GitHubCLI | None = None, gitlab: GitLabCLI | None = None) -> None:
        self.store = store
        self.task_service = task_service
        self.github = github or GitHubCLI()
        self.gitlab = gitlab or GitLabCLI()

    def check_due_reviews(self, interval_seconds: int, now: datetime | None = None) -> ReviewWatcherResult:
        current = now or datetime.now(UTC)
        checked_count = 0
        changed_count = 0
        conflict_count = 0
        merged_count = 0
        checked_ids: list[str] = []

        for promotion in self.store.list_promotions():
            if promotion.status != PromotionStatus.APPROVED:
                continue
            applyback = promotion.details.get("applyback") if isinstance(promotion.details, dict) else None
            if not isinstance(applyback, dict) or applyback.get("status") != "applied":
                continue
            if not applyback.get("pr_url"):
                continue
            watch = promotion.details.get("review_watch") if isinstance(promotion.details, dict) else None
            if isinstance(watch, dict) and watch.get("last_checked_at"):
                last_checked_at = datetime.fromisoformat(str(watch["last_checked_at"]))
                if (current - last_checked_at).total_seconds() < interval_seconds:
                    continue

            task = self.store.get_task(promotion.task_id)
            if task is None:
                continue
            project = self.store.get_project(task.project_id)
            if project is None or not project.repo_provider or not project.repo_name:
                continue

            branch_name = str(applyback.get("branch_name") or "")
            if not branch_name:
                continue

            status = self._fetch_status(project.repo_provider, project.repo_name, branch_name)
            if status is None:
                continue

            checked_count += 1
            checked_ids.append(promotion.id)
            previous_state = watch.get("state") if isinstance(watch, dict) else None
            previous_conflicts = watch.get("has_conflicts") if isinstance(watch, dict) else None
            review_watch = {
                "last_checked_at": current.isoformat(),
                "state": status["state"],
                "merge_state": status.get("merge_state"),
                "has_conflicts": bool(status.get("has_conflicts", False)),
                "url": status.get("url") or applyback.get("pr_url"),
                "branch_name": branch_name,
            }
            details = {**promotion.details, "review_watch": review_watch}
            updated = promotion.__class__(
                id=promotion.id,
                task_id=promotion.task_id,
                run_id=promotion.run_id,
                status=promotion.status,
                summary=promotion.summary,
                details=details,
                created_at=promotion.created_at,
            )
            self.store.update_promotion(updated)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=task.id,
                    event_type="promotion_review_synced",
                    payload={"promotion_id": promotion.id, **review_watch},
                )
            )
            if previous_state != review_watch["state"] or previous_conflicts != review_watch["has_conflicts"]:
                changed_count += 1
            if review_watch["has_conflicts"]:
                conflict_count += 1
                if previous_conflicts is not True:
                    follow_on_task_id = self._ensure_conflict_follow_on(task.id, promotion.run_id, branch_name, review_watch["url"])
                    self.store.create_event(
                        Event(
                            id=new_id("event"),
                            entity_type="task",
                            entity_id=task.id,
                            event_type="promotion_merge_conflict_detected",
                            payload={"promotion_id": promotion.id, "follow_on_task_id": follow_on_task_id, **review_watch},
                        )
                    )
            if review_watch["state"] == "merged":
                merged_count += 1
                if previous_state != "merged":
                    self.store.create_event(
                        Event(
                            id=new_id("event"),
                            entity_type="task",
                            entity_id=task.id,
                            event_type="promotion_merged",
                            payload={"promotion_id": promotion.id, **review_watch},
                        )
                    )

        return ReviewWatcherResult(
            checked_count=checked_count,
            changed_count=changed_count,
            conflict_count=conflict_count,
            merged_count=merged_count,
            checked_promotion_ids=checked_ids,
        )

    def _fetch_status(self, provider: RepoProvider, repo_name: str, branch_name: str) -> dict[str, object] | None:
        if provider == RepoProvider.GITHUB:
            return self.github.fetch_pull_request_status(repo_name, branch_name)
        if provider == RepoProvider.GITLAB:
            return self.gitlab.fetch_merge_request_status(repo_name, branch_name)
        return None

    def _ensure_conflict_follow_on(
        self,
        parent_task_id: str,
        source_run_id: str,
        branch_name: str,
        review_url: str | None,
    ) -> str | None:
        if self.task_service is None:
            return None
        existing = self.store.find_follow_on_task(parent_task_id, source_run_id)
        if existing is not None:
            return existing.id
        follow_on = self.task_service.create_follow_on_task(
            parent_task_id=parent_task_id,
            source_run_id=source_run_id,
            title="Rebase approved change onto current main",
            objective=(
                "Resolve the promotion merge conflict by replaying the approved change on top of the current base branch, "
                f"updating branch {branch_name}, and preserving the existing review context."
                + (f" Review URL: {review_url}." if review_url else "")
            ),
        )
        parent = self.store.get_task(parent_task_id)
        merged_metadata = dict(parent.external_ref_metadata) if parent is not None else {}
        merged_metadata["promotion_remediation"] = {
            "branch_name": branch_name,
            "review_url": review_url,
            "source_run_id": source_run_id,
        }
        self.store.update_task_external_metadata(follow_on.id, merged_metadata)
        return follow_on.id
