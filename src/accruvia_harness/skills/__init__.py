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
from .atomic_decomposition import AtomicDecompositionSkill
from .benchmark import BenchmarkSkill
from .cognition_heartbeat import CognitionHeartbeatSkill
from .commit import CommitSkill
from .diagnose import DiagnoseSkill
from .explain_failure import ExplainFailureSkill
from .fix_tests import FixTestsSkill
from .follow_on import FollowOnSkill
from .implement import ImplementSkill, apply_changes
from .interrogation import InterrogationSkill
from .mermaid_update_proposal import MermaidUpdateProposalSkill
from .context import SkillContext, RepoInventoryProvider, build_default_skill_context
from .plan_draft import PlanDraftSkill, PlanDraftTrioSkill, materialize_plans_from_skill_output
from .quality_gate import QualityGateSkill
from .review_plan_atomicity import ReviewPlanAtomicitySkill
from .post_merge_check import PostMergeCheckSkill
from .promotion_apply import PromotionApplySkill
from .promotion_review import PromotionReviewSkill
from .registry import SkillRegistry
from .reviewers import REVIEWER_SKILLS
from .sa_watch_triage import SAWatchTriageSkill
from .scope import ScopeSkill
from .self_review import SelfReviewSkill
from .summarize_run import SummarizeRunSkill
from .test_health import TestHealthSkill
from .translate_intent import TranslateIntentSkill
from .ui_responder import UIResponderSkill
from .validate import ValidateSkill, commands_for_profile
from .verify_acceptance import VerifyAcceptanceSkill


def build_default_registry(
    *, skill_context: SkillContext | None = None
) -> SkillRegistry:
    """Register all built-in skills.

    skill_context is required for context-aware skills (currently
    PlanDraftTrioSkill). If absent, the registry still returns but
    attempting to register PlanDraftTrioSkill will raise. This keeps
    legacy callers (tests, CLI tools that don't use TRIO) working
    while forcing new callers that need TRIO to supply a context.
    """
    registry = SkillRegistry()
    registry.register(ScopeSkill())
    registry.register(ImplementSkill())
    registry.register(SelfReviewSkill())
    registry.register(ValidateSkill())
    registry.register(DiagnoseSkill())
    registry.register(ExplainFailureSkill())
    registry.register(FixTestsSkill())
    registry.register(PromotionReviewSkill())
    registry.register(PromotionApplySkill())
    registry.register(PostMergeCheckSkill())
    registry.register(FollowOnSkill())
    registry.register(BenchmarkSkill())
    registry.register(CommitSkill())
    registry.register(SummarizeRunSkill())
    registry.register(TestHealthSkill())
    registry.register(TranslateIntentSkill())
    registry.register(QualityGateSkill())
    registry.register(VerifyAcceptanceSkill())
    registry.register(AtomicDecompositionSkill())
    registry.register(InterrogationSkill())
    registry.register(MermaidUpdateProposalSkill())
    registry.register(PlanDraftSkill())
    if skill_context is not None:
        registry.register(PlanDraftTrioSkill(context=skill_context))
    registry.register(ReviewPlanAtomicitySkill())
    registry.register(UIResponderSkill())
    registry.register(CognitionHeartbeatSkill())
    registry.register(SAWatchTriageSkill())
    for reviewer_cls in REVIEWER_SKILLS:
        registry.register(reviewer_cls())
    return registry


__all__ = [
    "AtomicDecompositionSkill",
    "BenchmarkSkill",
    "PlanDraftSkill",
    "PlanDraftTrioSkill",
    "RepoInventoryProvider",
    "ReviewPlanAtomicitySkill",
    "SkillContext",
    "build_default_skill_context",
    "materialize_plans_from_skill_output",
    "CognitionHeartbeatSkill",
    "CommitSkill",
    "InterrogationSkill",
    "MermaidUpdateProposalSkill",
    "REVIEWER_SKILLS",
    "SAWatchTriageSkill",
    "UIResponderSkill",
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
    "ExplainFailureSkill",
    "FixTestsSkill",
    "PromotionReviewSkill",
    "PromotionApplySkill",
    "PostMergeCheckSkill",
    "FollowOnSkill",
    "QualityGateSkill",
    "TranslateIntentSkill",
    "VerifyAcceptanceSkill",
    "apply_changes",
    "build_default_registry",
    "commands_for_profile",
    "extract_json_payload",
    "invoke_skill",
    "make_skill_context",
    "validate_against_schema",
]
