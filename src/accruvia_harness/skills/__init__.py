"""Skill framework for narrow, schema-bounded LLM roles.

Each skill = {prompt_template, output_schema, materialize_fn}. Skills produce
structured JSON output that downstream Python orchestration consumes. Mirrors
the pattern used by CognitionService.heartbeat(): the harness owns the prompt,
the LLM fills a narrow role, and deterministic code materializes the output.
"""
from __future__ import annotations

from .base import (
    Skill,
    SkillError,
    SkillInvocation,
    SkillResult,
    extract_json_payload,
    invoke_skill,
    make_skill_context,
    validate_against_schema,
)
from .benchmark import BenchmarkSkill
from .commit import CommitSkill
from .diagnose import DiagnoseSkill
from .follow_on import FollowOnSkill
from .implement import ImplementSkill, apply_changes
from .post_merge_check import PostMergeCheckSkill
from .promotion_apply import PromotionApplySkill
from .promotion_review import PromotionReviewSkill
from .registry import SkillRegistry
from .scope import ScopeSkill
from .self_review import SelfReviewSkill
from .summarize_run import SummarizeRunSkill
from .test_health import TestHealthSkill
from .validate import ValidateSkill, commands_for_profile


def build_default_registry() -> SkillRegistry:
    """Register all built-in skills."""
    registry = SkillRegistry()
    registry.register(ScopeSkill())
    registry.register(ImplementSkill())
    registry.register(SelfReviewSkill())
    registry.register(ValidateSkill())
    registry.register(DiagnoseSkill())
    registry.register(PromotionReviewSkill())
    registry.register(PromotionApplySkill())
    registry.register(PostMergeCheckSkill())
    registry.register(FollowOnSkill())
    registry.register(BenchmarkSkill())
    registry.register(CommitSkill())
    registry.register(SummarizeRunSkill())
    registry.register(TestHealthSkill())
    return registry


__all__ = [
    "BenchmarkSkill",
    "CommitSkill",
    "Skill",
    "SkillError",
    "SkillInvocation",
    "SkillResult",
    "SkillRegistry",
    "ScopeSkill",
    "ImplementSkill",
    "SelfReviewSkill",
    "SummarizeRunSkill",
    "TestHealthSkill",
    "ValidateSkill",
    "DiagnoseSkill",
    "PromotionReviewSkill",
    "PromotionApplySkill",
    "PostMergeCheckSkill",
    "FollowOnSkill",
    "apply_changes",
    "build_default_registry",
    "commands_for_profile",
    "extract_json_payload",
    "invoke_skill",
    "make_skill_context",
    "validate_against_schema",
]
