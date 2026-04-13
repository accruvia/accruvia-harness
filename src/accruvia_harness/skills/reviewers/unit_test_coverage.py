"""Unit-test coverage reviewer."""
from __future__ import annotations

from .base import BaseReviewerSkill


class ReviewUnitTestCoverageSkill(BaseReviewerSkill):
    name = "review_unit_test_coverage"
    dimension = "unit_test_coverage"
    reviewer_label = "unit_test_coverage_reviewer"
    dimension_emphasis = (
        "Check that unit tests exist for every new or modified code path. A break-fix\n"
        "without a companion unit test is a remediation_required finding. Look for\n"
        "tests that actually exercise the new behaviour, not placeholder asserts."
    )
