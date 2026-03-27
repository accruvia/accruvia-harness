"""Harness-side exploration policy (Step 4).

Decides which model/backend to use for each invocation.  The first version
uses epsilon-greedy: with probability ``epsilon`` a random allowed model is
chosen (exploration), otherwise the best-known model is chosen (exploitation).

Design rules:
- Exploration lives in the harness, not in CLI wrappers.
- High-risk tasks can disable exploration entirely.
- Every decision is fully auditable via ``PolicyDecision``.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from routellect.protocols import ModelCapability, RoutingDecision
from routellect.routing_events import ModelUniverseSnapshot


@dataclass
class PolicyDecision:
    """Fully-auditable routing decision produced by the policy layer."""

    routing_decision: RoutingDecision
    exploration_flag: bool
    rationale: str
    universe_snapshot_id: str
    task_fingerprint: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class EpsilonGreedyPolicy:
    """Simple epsilon-greedy exploration policy.

    Parameters
    ----------
    epsilon : float
        Probability of exploring (0.0–1.0).  Default 0.1 (10%).
    rng : random.Random | None
        Optional seeded RNG for deterministic tests.
    high_risk_profiles : set[str]
        Validation profiles that disable exploration entirely.
    """

    def __init__(
        self,
        epsilon: float = 0.1,
        rng: random.Random | None = None,
        high_risk_profiles: set[str] | None = None,
    ) -> None:
        self.epsilon = epsilon
        self.rng = rng or random.Random()
        self.high_risk_profiles = high_risk_profiles or set()

    def select(
        self,
        task_fingerprint: dict[str, Any],
        universe: ModelUniverseSnapshot,
        prior_outcomes: list[dict[str, Any]] | None = None,
        validation_profile: str = "generic",
    ) -> PolicyDecision:
        """Pick a model from the universe.

        Parameters
        ----------
        task_fingerprint : dict
            Opaque task description used for stratification.
        universe : ModelUniverseSnapshot
            Current available models.
        prior_outcomes : list[dict] | None
            Historical outcomes for ranking (exploit path).
        validation_profile : str
            If in ``high_risk_profiles``, exploration is suppressed.
        """
        available = [m for m in universe.models if m.available]
        if not available:
            raise ValueError("No available models in the universe")

        disable_exploration = validation_profile in self.high_risk_profiles
        explore = (not disable_exploration) and (self.rng.random() < self.epsilon)

        if explore:
            chosen = self.rng.choice(available)
            return PolicyDecision(
                routing_decision=RoutingDecision(
                    model_id=chosen.model_id,
                    backend=chosen.backend,
                    confidence=0.0,
                    reasoning="epsilon-greedy exploration",
                    universe_hash=universe.snapshot_id,
                    is_exploration=True,
                ),
                exploration_flag=True,
                rationale=f"Exploring: randomly selected {chosen.model_id} from {len(available)} candidates",
                universe_snapshot_id=universe.snapshot_id,
                task_fingerprint=task_fingerprint,
            )

        # Exploit: pick the best model based on prior outcomes.
        best = self._best_model(available, prior_outcomes or [])
        return PolicyDecision(
            routing_decision=RoutingDecision(
                model_id=best.model_id,
                backend=best.backend,
                confidence=1.0,
                reasoning="epsilon-greedy exploitation (best known model)",
                universe_hash=universe.snapshot_id,
                is_exploration=False,
            ),
            exploration_flag=False,
            rationale=f"Exploiting: selected {best.model_id} as best known model",
            universe_snapshot_id=universe.snapshot_id,
            task_fingerprint=task_fingerprint,
        )

    def _best_model(
        self,
        available: list[ModelCapability],
        prior_outcomes: list[dict[str, Any]],
    ) -> ModelCapability:
        """Rank models by success first, then by cost/tokens/latency efficiency."""
        if not prior_outcomes:
            return available[0]

        success_counts: dict[str, int] = {}
        total_counts: dict[str, int] = {}
        cost_sums: dict[str, float] = {}
        token_sums: dict[str, float] = {}
        latency_sums: dict[str, float] = {}
        metric_counts: dict[str, int] = {}

        def _to_non_negative_float(value: Any) -> float | None:
            if value is None:
                return None
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(numeric) or numeric < 0.0:
                return None
            return numeric

        for outcome in prior_outcomes:
            mid = outcome.get("model_id", "")
            if not mid:
                continue
            total_counts[mid] = total_counts.get(mid, 0) + 1
            if outcome.get("success"):
                success_counts[mid] = success_counts.get(mid, 0) + 1
            cost = _to_non_negative_float(outcome.get("llm_cost_usd"))
            total_tokens = _to_non_negative_float(outcome.get("llm_total_tokens"))
            latency = _to_non_negative_float(outcome.get("llm_latency_ms"))
            if cost is None and "cost" in outcome:
                cost = _to_non_negative_float(outcome.get("cost"))
            if total_tokens is None and ("input_tokens" in outcome or "output_tokens" in outcome):
                input_tokens = _to_non_negative_float(outcome.get("input_tokens")) or 0.0
                output_tokens = _to_non_negative_float(outcome.get("output_tokens")) or 0.0
                total_tokens = input_tokens + output_tokens
            if latency is None and "latency_ms" in outcome:
                latency = _to_non_negative_float(outcome.get("latency_ms"))
            if cost is None and total_tokens is None and latency is None:
                continue
            metric_counts[mid] = metric_counts.get(mid, 0) + 1
            cost_sums[mid] = cost_sums.get(mid, 0.0) + (cost or 0.0)
            token_sums[mid] = token_sums.get(mid, 0.0) + (total_tokens or 0.0)
            latency_sums[mid] = latency_sums.get(mid, 0.0) + (latency or 0.0)

        total_metric_rows = sum(metric_counts.values())
        global_cost_avg = sum(cost_sums.values()) / total_metric_rows if total_metric_rows else 1.0
        global_tokens_avg = sum(token_sums.values()) / total_metric_rows if total_metric_rows else 1.0
        global_latency_avg = sum(latency_sums.values()) / total_metric_rows if total_metric_rows else 1.0

        def _score(model: ModelCapability) -> tuple[float, str]:
            total = total_counts.get(model.model_id, 0)
            if total == 0:
                # Mildly optimistic prior for untried models.
                return (0.7, model.model_id)

            success_rate = success_counts.get(model.model_id, 0) / total
            metric_count = metric_counts.get(model.model_id, 0)
            avg_cost = (
                cost_sums.get(model.model_id, 0.0) / metric_count
                if metric_count
                else global_cost_avg
            )
            avg_tokens = (
                token_sums.get(model.model_id, 0.0) / metric_count
                if metric_count
                else global_tokens_avg
            )
            avg_latency = (
                latency_sums.get(model.model_id, 0.0) / metric_count
                if metric_count
                else global_latency_avg
            )

            cost_factor = avg_cost / max(global_cost_avg, 1e-9)
            token_factor = avg_tokens / max(global_tokens_avg, 1e-9)
            latency_factor = avg_latency / max(global_latency_avg, 1e-9)
            # Lower score is better. Success dominates, metrics are tie-breakers.
            score = (
                (1.0 - success_rate) * 0.6
                + cost_factor * 0.2
                + token_factor * 0.1
                + latency_factor * 0.1
            )
            return (score, model.model_id)

        return min(available, key=_score)
