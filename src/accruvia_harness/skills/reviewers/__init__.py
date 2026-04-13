"""Objective-review reviewer skills.

One skill per review dimension. Each skill emits a single packet matching
the shape consumed by HarnessUIDataService objective-review downstream code.
"""
from __future__ import annotations

from .atomic_fidelity import ReviewAtomicFidelitySkill
from .base import BaseReviewerSkill
from .code_structure import ReviewCodeStructureSkill
from .devops import ReviewDevOpsSkill
from .integration_e2e_coverage import ReviewIntegrationE2ESkill
from .intent_fidelity import ReviewIntentFidelitySkill
from .security import ReviewSecuritySkill
from .unit_test_coverage import ReviewUnitTestCoverageSkill

REVIEWER_SKILLS: list[type[BaseReviewerSkill]] = [
    ReviewIntentFidelitySkill,
    ReviewUnitTestCoverageSkill,
    ReviewIntegrationE2ESkill,
    ReviewSecuritySkill,
    ReviewDevOpsSkill,
    ReviewAtomicFidelitySkill,
    ReviewCodeStructureSkill,
]

__all__ = [
    "BaseReviewerSkill",
    "REVIEWER_SKILLS",
    "ReviewAtomicFidelitySkill",
    "ReviewCodeStructureSkill",
    "ReviewDevOpsSkill",
    "ReviewIntegrationE2ESkill",
    "ReviewIntentFidelitySkill",
    "ReviewSecuritySkill",
    "ReviewUnitTestCoverageSkill",
]
