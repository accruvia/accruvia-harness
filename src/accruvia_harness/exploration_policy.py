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
        """Rank available models by historical success rate, break ties by order."""
        if not prior_outcomes:
            return available[0]

        success_counts: dict[str, int] = {}
        total_counts: dict[str, int] = {}
        for outcome in prior_outcomes:
            mid = outcome.get("model_id", "")
            total_counts[mid] = total_counts.get(mid, 0) + 1
            if outcome.get("success"):
                success_counts[mid] = success_counts.get(mid, 0) + 1

        def _score(model: ModelCapability) -> float:
            total = total_counts.get(model.model_id, 0)
            if total == 0:
                return 0.5  # Optimistic prior for untried models
            return success_counts.get(model.model_id, 0) / total

        return max(available, key=_score)
