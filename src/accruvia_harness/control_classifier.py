"""Failure classification — skill-backed replacement for the old keyword classifier.

This module preserves the `FailureClassifier` name and `.classify(evidence)`
surface so control_runtime, control_watch, and tests continue to work. The
underlying logic is now:

1. A deterministic fast-path for unambiguous infrastructure failures (rate
   limits, timeouts, credit exhaustion, artifact contract failures, ...).
   These are cheap and must be cheap — they are called on every failure event.

2. An optional LLM-backed `/diagnose` skill that handles novel or ambiguous
   evidence that the fast-path could not classify. This replaces the old
   `unknown` classification for anything the keyword matcher could not see.

When no LLM router is configured, the classifier returns fast-path results
and falls back to `unknown` — matching the behavior of the prior classifier.
When an LLM router is configured, `/diagnose` is invoked for anything the
fast-path could not confidently classify.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .domain import FailureClassification, Run, RunStatus, Task, TaskStatus, new_id

if TYPE_CHECKING:  # pragma: no cover
    from .llm import LLMRouter
    from .skills.diagnose import DiagnoseSkill


# Deterministic patterns. Ordered by specificity; first match wins.
# Each entry: (required_substrings, classification, cooldown_seconds, retry_recommended, confidence)
_FAST_PATH: tuple[tuple[tuple[str, ...], str, int, bool, float], ...] = (
    (("rate limit",), "provider_rate_limit", 1800, False, 0.95),
    (("429",), "provider_rate_limit", 1800, False, 0.95),
    (("credit", "exhaust"), "credit_exhaustion", 0, False, 0.95),
    (("credit", "insufficient"), "credit_exhaustion", 0, False, 0.95),
    (("quota exceeded",), "credit_exhaustion", 0, False, 0.95),
    (("connection refused",), "provider_outage", 1800, False, 0.8),
    (("service unavailable",), "provider_outage", 1800, False, 0.8),
    (("503",), "provider_outage", 1800, False, 0.8),
    (("timed out",), "timeout", 0, True, 0.85),
    (("timeout",), "timeout", 0, True, 0.8),
    (("missing required artifact",), "artifact_contract_failure", 0, True, 0.9),
    (("missing required artifacts",), "artifact_contract_failure", 0, True, 0.9),
    (("validation_evidence_missing",), "artifact_contract_failure", 0, True, 0.9),
    (("artifacts were insufficient",), "artifact_contract_failure", 0, True, 0.9),
    (("required artifact",), "artifact_contract_failure", 0, True, 0.85),
    (("objective_review_packet",), "artifact_contract_failure", 0, True, 0.85),
    (("retry budget exhausted",), "system_failure", 0, False, 0.8),
    (("killed",), "hung_process", 0, True, 0.7),
    (("stale",), "hung_process", 0, True, 0.7),
    (("hung",), "hung_process", 0, True, 0.7),
)


def _fast_classify(evidence: str) -> FailureClassification | None:
    if not evidence:
        return None
    haystack = evidence.lower()
    for keywords, classification, cooldown, retry, confidence in _FAST_PATH:
        if all(kw in haystack for kw in keywords):
            return FailureClassification(
                classification=classification,
                confidence=confidence,
                retry_recommended=retry,
                cooldown_seconds=cooldown,
                evidence=[evidence[:500]],
            )
    return None


def _shallow_signal_classification(haystack: str, evidence_list: list[str]) -> FailureClassification:
    """Last-resort hint when LLM is unavailable and fast-path missed."""
    if "traceback" in haystack or "exception" in haystack or "error:" in haystack:
        return FailureClassification(
            classification="system_failure",
            confidence=0.5,
            retry_recommended=False,
            cooldown_seconds=0,
            evidence=evidence_list,
        )
    return FailureClassification(
        classification="unknown",
        confidence=0.2,
        retry_recommended=False,
        cooldown_seconds=0,
        evidence=evidence_list,
    )


class FailureClassifier:
    """Classifies failure evidence, preferring a fast deterministic path and
    falling back to the `/diagnose` skill for ambiguous evidence.

    Constructor args are optional so existing test code like
    `FailureClassifier()` continues to work without an LLM.
    """

    def __init__(
        self,
        llm_router: "LLMRouter | None" = None,
        workspace_root: Path | None = None,
        telemetry: Any = None,
        diagnose_skill: "DiagnoseSkill | None" = None,
        project_id_hint: str = "system",
    ) -> None:
        self.llm_router = llm_router
        self.workspace_root = workspace_root
        self.telemetry = telemetry
        self._diagnose_skill = diagnose_skill
        self.project_id_hint = project_id_hint
        # prior diagnoses for recurrence awareness
        self._recent: list[str] = []

    def classify(
        self,
        evidence: str | None,
        *,
        extra_evidence: list[str] | None = None,
        context: str = "",
        attempt: int = 1,
    ) -> FailureClassification:
        snippets = [item for item in [evidence, *(extra_evidence or [])] if item]
        joined = "\n".join(snippets)
        fast = _fast_classify(joined)
        if fast is not None:
            self._note(fast.classification)
            return fast

        if self.llm_router is not None and getattr(self.llm_router, "executors", {}):
            llm_result = self._llm_classify(joined, context=context, attempt=attempt)
            if llm_result is not None:
                self._note(llm_result.classification)
                return llm_result

        fallback = _shallow_signal_classification(joined.lower(), snippets[:5])
        self._note(fallback.classification)
        return fallback

    def classify_paths(self, *paths: str | Path) -> FailureClassification:
        snippets: list[str] = []
        for path in paths:
            file_path = Path(path)
            if not file_path.exists():
                continue
            try:
                snippets.append(file_path.read_text(encoding="utf-8")[-4000:])
            except OSError:
                continue
        return self.classify(None, extra_evidence=snippets)

    def _llm_classify(
        self,
        evidence: str,
        *,
        context: str,
        attempt: int,
    ) -> FailureClassification | None:
        skill = self._get_diagnose_skill()
        workspace_root = self.workspace_root or Path(".accruvia-harness") / "workspace"
        run_dir = workspace_root / "skills" / "diagnose" / new_id("diag")
        task = Task(
            id=new_id("diagnose_task"),
            project_id=self.project_id_hint,
            title="Failure diagnosis",
            objective="Classify failure evidence for control plane policy.",
            strategy="diagnose",
            status=TaskStatus.COMPLETED,
        )
        run = Run(
            id=new_id("diagnose_run"),
            task_id=task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary="Diagnose failure evidence",
        )
        from .skills.base import SkillInvocation, invoke_skill

        invocation = SkillInvocation(
            skill_name=skill.name,
            inputs={
                "evidence": evidence,
                "context": context,
                "attempt": attempt,
                "prior_diagnoses": tuple(self._recent[-3:]),
            },
            task=task,
            run=run,
            run_dir=run_dir,
        )
        result = invoke_skill(skill, invocation, self.llm_router, telemetry=self.telemetry)
        if not result.success:
            return None
        return skill.to_classification(result.output, evidence)

    def _get_diagnose_skill(self) -> "DiagnoseSkill":
        if self._diagnose_skill is None:
            from .skills.diagnose import DiagnoseSkill

            self._diagnose_skill = DiagnoseSkill()
        return self._diagnose_skill

    def _note(self, classification: str) -> None:
        self._recent.append(classification)
        if len(self._recent) > 8:
            self._recent = self._recent[-8:]
