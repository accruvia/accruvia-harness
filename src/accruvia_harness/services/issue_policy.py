from __future__ import annotations

from dataclasses import dataclass

from ..domain import PromotionStatus, TaskStatus


@dataclass(slots=True)
class IssueStatePolicy:
    close_on_completed: bool = True
    close_only_on_approved_promotion: bool = False
    reopen_on_pending: bool = True
    reopen_on_active: bool = True
    reopen_on_failed: bool = True

    def desired_state(self, task_status: TaskStatus, latest_promotion_status: PromotionStatus | None) -> str:
        if (
            task_status == TaskStatus.COMPLETED
            and self.close_on_completed
            and (
                not self.close_only_on_approved_promotion
                or latest_promotion_status == PromotionStatus.APPROVED
            )
        ):
            return "closed"
        if task_status == TaskStatus.PENDING and self.reopen_on_pending:
            return "open"
        if task_status == TaskStatus.ACTIVE and self.reopen_on_active:
            return "open"
        if task_status == TaskStatus.FAILED and self.reopen_on_failed:
            return "open"
        return "unchanged"
