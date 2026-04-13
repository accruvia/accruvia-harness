"""Atomic-fidelity reviewer: is each task actually atomic?"""
from __future__ import annotations

from .base import BaseReviewerSkill


class ReviewAtomicFidelitySkill(BaseReviewerSkill):
    name = "review_atomic_fidelity"
    dimension = "atomic_fidelity"
    reviewer_label = "atomic_fidelity_reviewer"
    dimension_emphasis = (
        "Check that every linked task corresponds to a single atomic unit (one function\n"
        "or one tightly-coupled page of code). Flag any task whose diff sprawls across\n"
        "unrelated modules — that is an atomicity violation that hides regressions."
    )
