"""Security reviewer."""
from __future__ import annotations

from .base import BaseReviewerSkill


class ReviewSecuritySkill(BaseReviewerSkill):
    name = "review_security"
    dimension = "security"
    reviewer_label = "security_reviewer"
    dimension_emphasis = (
        "Check for secret leakage, missing auth checks, unsafe deserialisation, command\n"
        "injection, path traversal, and any new attack surface introduced by the diff.\n"
        "Flag lack of input validation on operator-supplied paths."
    )
