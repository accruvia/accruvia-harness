"""Intent-fidelity reviewer: does the work satisfy the operator's intent?"""
from __future__ import annotations

from .base import BaseReviewerSkill


class ReviewIntentFidelitySkill(BaseReviewerSkill):
    name = "review_intent_fidelity"
    dimension = "intent_fidelity"
    reviewer_label = "intent_fidelity_reviewer"
    dimension_emphasis = (
        "Check that the implementation actually satisfies the operator's stated intent\n"
        "and success definition. Watch for non-negotiable violations and silent scope\n"
        "drift. A 'pass' verdict requires that every non-negotiable is observably honoured\n"
        "by the diff or the linked tasks."
    )
