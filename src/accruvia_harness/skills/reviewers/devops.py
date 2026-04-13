"""DevOps / operability reviewer."""
from __future__ import annotations

from .base import BaseReviewerSkill


class ReviewDevOpsSkill(BaseReviewerSkill):
    name = "review_devops"
    dimension = "devops"
    reviewer_label = "devops_reviewer"
    dimension_emphasis = (
        "Check operability: configuration changes, migrations, telemetry, error budgets,\n"
        "and rollback safety. New behaviour must be observable and reversible. Flag any\n"
        "schema or migration change that lacks rollback notes."
    )
