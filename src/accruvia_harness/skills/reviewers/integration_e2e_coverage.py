"""Integration / end-to-end coverage reviewer."""
from __future__ import annotations

from .base import BaseReviewerSkill


class ReviewIntegrationE2ESkill(BaseReviewerSkill):
    name = "review_integration_e2e_coverage"
    dimension = "integration_e2e_coverage"
    reviewer_label = "integration_e2e_reviewer"
    dimension_emphasis = (
        "Check that end-to-end / integration tests exercise the workflow change. If the\n"
        "objective changes a user-facing flow, an integration or workflow test must cover\n"
        "the new path. Unit tests alone are insufficient for cross-component changes."
    )
