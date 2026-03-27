"""Harness-side exploration policy (Step 4).

Decides which model/backend to use for each invocation.  The first version
uses epsilon-greedy: with probability ``epsilon`` a random allowed model is
chosen (exploration), otherwise the best-known model is chosen (exploitation).

Design rules:
- Exploration lives in the harness, not in CLI wrappers.
- High-risk tasks can disable exploration entirely.
- Every decision is fully auditable via ``PolicyDecision``.

Token-performance demotion (feat/token-performance-demotion):
- ``_best_model`` now scores models on a composite metric combining:
    success rate (weight 0.5), cost efficiency (0.3), and latency (0.2).
- Cost and latency scores are computed relative to the best observed value
  across the candidate set so the metric is dimensionless and comparable.
- Models with no token data fall back to the optimistic prior (0.5) for
  cost and latency components so they are not unfairly penalised.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from routellect.protocols import ModelCapability, RoutingDecision
from routellect.routing_events import ModelUniverseSnapshot

# Composite scoring weights.  Must sum to 1.0.
_WEIGHT_SUCCESS = 0.5
_WEIGHT_COST = 0.3
_WEIGHT_LATENCY = 0.2


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
            Historical outcomes for ranking (exploit path).  Each entry may
            include token-performance keys emitted by the harness executor:
            ``model_id``, ``success`` (bool), ``llm_cost_usd`` (float),
            ``llm_total_tokens`` (int), ``llm_latency_ms`` (float).
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
        """Rank available models by composite score; break ties by order.

        Composite score = 0.5 * success_rate
                        + 0.3 * cost_efficiency   (lower cost → higher score)
                        + 0.2 * latency_efficiency (lower latency → higher score)

        Cost and latency scores are normalised relative to the best (lowest)
        observed value so the metric is dimensionless.  Models with no
        observed data receive an optimistic prior of 0.5 on each component.
        """
        if not prior_outcomes:
            return available[0]

        # Aggregate per-model statistics from prior outcomes.
        success_counts: dict[str, int] = {}
        total_counts: dict[str, int] = {}
        cost_totals: dict[str, float] = {}
        latency_totals: dict[str, float] = {}

        for outcome in prior_outcomes:
            mid = outcome.get("model_id", "")
            if not mid:
                continue
            total_counts[mid] = total_counts.get(mid, 0) + 1
            if outcome.get("success"):
                success_counts[mid] = success_counts.get(mid, 0) + 1
            cost = outcome.get("llm_cost_usd") or outcome.get("cost_usd") or 0.0
            latency = outcome.get("llm_latency_ms") or outcome.get("latency_ms") or 0.0
            cost_totals[mid] = cost_totals.get(mid, 0.0) + float(cost)
            latency_totals[mid] = latency_totals.get(mid, 0.0) + float(latency)

        # Compute per-model averages for cost and latency.
        avg_cost: dict[str, float] = {}
        avg_latency: dict[str, float] = {}
        for mid, total in total_counts.items():
            if total > 0:
                avg_cost[mid] = cost_totals.get(mid, 0.0) / total
                avg_latency[mid] = latency_totals.get(mid, 0.0) / total

        # Reference values for normalisation (best = lowest).
        observed_costs = [v for v in avg_cost.values() if v > 0]
        observed_latencies = [v for v in avg_latency.values() if v > 0]
        best_cost = min(observed_costs) if observed_costs else None
        best_latency = min(observed_latencies) if observed_latencies else None

        def _cost_score(mid: str) -> float:
            """Higher score = lower cost relative to best observed."""
            if best_cost is None or mid not in avg_cost or avg_cost[mid] <= 0:
                return 0.5  # Optimistic prior for untried / zero-cost models
            return best_cost / avg_cost[mid]

        def _latency_score(mid: str) -> float:
            """Higher score = lower latency relative to best observed."""
            if best_latency is None or mid not in avg_latency or avg_latency[mid] <= 0:
                return 0.5  # Optimistic prior
            return best_latency / avg_latency[mid]

        def _success_rate(mid: str) -> float:
            total = total_counts.get(mid, 0)
            if total == 0:
                return 0.5  # Optimistic prior for untried models
            return success_counts.get(mid, 0) / total

        def _composite_score(model: ModelCapability) -> float:
            mid = model.model_id
            return (
                _WEIGHT_SUCCESS * _success_rate(mid)
                + _WEIGHT_COST * _cost_score(mid)
                + _WEIGHT_LATENCY * _latency_score(mid)
            )

        return max(available, key=_composite_score)
