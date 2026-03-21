from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class RedTeamRound:
    attempt: int
    candidate_summary: str
    candidate_content: str
    review_ready_for_human_review: bool
    major_findings: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class RedTeamLoopResult:
    candidate: dict[str, str]
    generation_metadata: dict[str, object]
    latest_review: dict[str, object] | None
    history: list[RedTeamRound]


class RedTeamLoopService:
    def run(
        self,
        *,
        initial_prompt: str,
        max_rounds: int,
        generate_candidate: Callable[[str, int, list[RedTeamRound]], tuple[Any, dict[str, object]] | None],
        review_candidate: Callable[[Any, int, list[RedTeamRound]], dict[str, object]] | None = None,
        build_retry_prompt: Callable[[str, Any, list[dict[str, object]], list[RedTeamRound], int, int], str] | None = None,
        candidate_summary: Callable[[Any], str] | None = None,
        candidate_content: Callable[[Any], str] | None = None,
    ) -> RedTeamLoopResult | None:
        prompt = initial_prompt
        latest_review: dict[str, object] | None = None
        latest_candidate: Any = None
        latest_generation_metadata: dict[str, object] = {}
        history: list[RedTeamRound] = []

        for attempt in range(1, max_rounds + 1):
            generated = generate_candidate(prompt, attempt, history)
            if generated is None:
                return None
            latest_candidate, latest_generation_metadata = generated
            if review_candidate is None:
                break

            latest_review = review_candidate(latest_candidate, attempt, history)
            deterministic_findings = list((latest_review.get("deterministic_review") or {}).get("findings") or [])
            llm_findings = list((latest_review.get("llm_review") or {}).get("findings") or [])
            major_findings = [
                item
                for item in deterministic_findings + llm_findings
                if str(item.get("severity") or "").lower() in {"critical", "major"}
            ]
            history.append(
                RedTeamRound(
                    attempt=attempt,
                    candidate_summary=(candidate_summary(latest_candidate) if candidate_summary else _default_candidate_summary(latest_candidate)),
                    candidate_content=(candidate_content(latest_candidate) if candidate_content else _default_candidate_content(latest_candidate)),
                    review_ready_for_human_review=bool(latest_review.get("ready_for_human_review", False)),
                    major_findings=major_findings,
                )
            )
            if latest_review.get("ready_for_human_review", False) and not major_findings:
                break
            if attempt >= max_rounds or build_retry_prompt is None:
                break
            prompt = build_retry_prompt(
                initial_prompt,
                latest_candidate,
                major_findings,
                history,
                attempt,
                max_rounds,
            )

        if latest_candidate is None:
            return None
        return RedTeamLoopResult(
            candidate=latest_candidate,
            generation_metadata=latest_generation_metadata,
            latest_review=latest_review,
            history=history,
        )


def _default_candidate_summary(candidate: Any) -> str:
    if isinstance(candidate, dict):
        return str(candidate.get("summary") or "")[:200]
    return str(candidate)[:200]


def _default_candidate_content(candidate: Any) -> str:
    if isinstance(candidate, dict):
        return str(candidate.get("content") or "")
    return str(candidate)
