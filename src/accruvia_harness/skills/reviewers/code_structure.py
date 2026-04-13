"""Code-structure reviewer."""
from __future__ import annotations

from .base import BaseReviewerSkill


class ReviewCodeStructureSkill(BaseReviewerSkill):
    name = "review_code_structure"
    dimension = "code_structure"
    reviewer_label = "code_structure_reviewer"
    dimension_emphasis = (
        "Check structural quality: layering, separation of concerns, naming, dead code,\n"
        "and band-aids. Flag any new module-level singleton, any global mutable state, or\n"
        "any inline duplication of an existing helper."
    )
