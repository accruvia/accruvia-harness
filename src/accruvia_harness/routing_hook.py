"""Routing hook: wires model discovery, exploration, and safety into the
harness execution flow (Steps 3, 6, 7).

This module is the single integration point between the harness and
routellect.  It is called during harness bootstrap and before each
LLM invocation.

Usage in engine bootstrap::

    from accruvia_harness.routing_hook import RoutingHook
    hook = RoutingHook.from_config(config)
    # Before each invocation:
    invocation.model = hook.select_model_for(task, run).model_id
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from routellect.protocols import ModelCapability, RoutingDecision, RoutingOutcome
from routellect.routing_events import (
    ModelUniverseSnapshot,
    RoutingDecisionEvent,
    RoutingOutcomeEvent,
)

from .config import HarnessConfig
from .exploration_policy import EpsilonGreedyPolicy, PolicyDecision
from .model_inventory import (
    discover_available_models,
    load_universe_cache,
    save_universe_cache,
)

logger = logging.getLogger(__name__)

_FALLBACK_MODELS_BY_BACKEND = {
    "codex": ModelCapability(
        backend="codex",
        provider="openai",
        model_id="gpt-5.4",
        supports_streaming=True,
        supports_tools=True,
        available=True,
    ),
    "claude": ModelCapability(
        backend="claude",
        provider="anthropic",
        model_id="claude-sonnet-4-6",
        supports_streaming=True,
        supports_tools=True,
        available=True,
    ),
    "accruvia_client": ModelCapability(
        backend="accruvia_client",
        provider="anthropic",
        model_id="accruvia-client-default",
        supports_streaming=True,
        supports_tools=True,
        available=True,
    ),
    "command": ModelCapability(
        backend="command",
        provider="unknown",
        model_id="command-default",
        supports_streaming=False,
        supports_tools=False,
        available=True,
    ),
}


class RoutingHook:
    """Single integration point between harness and routellect routing."""

    def __init__(
        self,
        universe: ModelUniverseSnapshot,
        policy: EpsilonGreedyPolicy,
        cache_path: Path | None = None,
    ) -> None:
        self.universe = universe
        self.policy = policy
        self.cache_path = cache_path
        self._event_log: list[dict[str, Any]] = []

    @classmethod
    def from_config(
        cls,
        config: HarnessConfig,
        epsilon: float = 0.0,
        high_risk_profiles: set[str] | None = None,
    ) -> "RoutingHook":
        """Bootstrap routing from harness config.

        1. Determine which backends are configured.
        2. Discover available models (or load from cache).
        3. Build the universe snapshot.
        4. Return a ready-to-use hook.
        """
        configured_backends = _configured_backends(config)
        cache_path = config.telemetry_dir / "model_universe_cache.json"

        models = discover_available_models(configured_backends)
        if not any(m.available and m.probe_error is None for m in models):
            cached = load_universe_cache(cache_path)
            if cached:
                logger.warning("All probes failed — using cached universe (%d models)", len(cached))
                models = cached
            else:
                logger.warning("All probes failed and no cache — using configured fallback universe")
                models = _fallback_universe(configured_backends, config)

        universe = ModelUniverseSnapshot(models=models)
        save_universe_cache(models, cache_path)

        policy = EpsilonGreedyPolicy(
            epsilon=epsilon,
            high_risk_profiles=high_risk_profiles or set(),
        )

        return cls(universe=universe, policy=policy, cache_path=cache_path)

    def select_model_for(
        self,
        task_fingerprint: dict[str, Any],
        validation_profile: str = "generic",
        prior_outcomes: list[dict[str, Any]] | None = None,
    ) -> PolicyDecision:
        """Select a model for an invocation, with safety checks."""
        decision = self.policy.select(
            task_fingerprint=task_fingerprint,
            universe=self.universe,
            prior_outcomes=prior_outcomes,
            validation_profile=validation_profile,
        )

        # Safety rail (Step 7): reject decisions for models not in the
        # current universe.
        known_ids = {m.model_id for m in self.universe.models if m.available}
        if decision.routing_decision.model_id not in known_ids:
            logger.warning(
                "Routing returned model '%s' not in current universe — using fallback",
                decision.routing_decision.model_id,
            )
            fallback = next((m for m in self.universe.models if m.available), None)
            if fallback is None:
                raise ValueError("No available models in the universe for fallback")
            decision = PolicyDecision(
                routing_decision=RoutingDecision(
                    model_id=fallback.model_id,
                    backend=fallback.backend,
                    confidence=0.0,
                    reasoning="safety fallback — selected model was not in universe",
                    universe_hash=self.universe.snapshot_id,
                    is_exploration=False,
                ),
                exploration_flag=False,
                rationale="Fallback after universe mismatch",
                universe_snapshot_id=self.universe.snapshot_id,
                task_fingerprint=task_fingerprint,
            )

        # Log the decision event.
        event = RoutingDecisionEvent(
            task_fingerprint=task_fingerprint,
            decision=decision.routing_decision,
            universe_snapshot_id=self.universe.snapshot_id,
        )
        self._event_log.append(event.to_dict())
        return decision

    def record_outcome(
        self,
        decision: RoutingDecision,
        outcome: RoutingOutcome,
        token_metrics: dict[str, float] | None = None,
    ) -> None:
        """Record an invocation outcome for learning and audit."""
        event = RoutingOutcomeEvent(
            decision=decision,
            outcome=outcome,
            universe_snapshot_id=self.universe.snapshot_id,
        )
        payload = event.to_dict()
        if token_metrics:
            payload["token_metrics"] = dict(token_metrics)
        self._event_log.append(payload)

    def get_event_log(self) -> list[dict[str, Any]]:
        """Return all routing events emitted during this session."""
        return list(self._event_log)


def _configured_backends(config: HarnessConfig) -> list[str]:
    """Determine which LLM backends have commands configured."""
    backends: list[str] = []
    if config.llm_codex_command:
        backends.append("codex")
    if config.llm_claude_command:
        backends.append("claude")
    if config.llm_accruvia_client_command:
        backends.append("accruvia_client")
    if config.llm_command:
        backends.append("command")
    return backends


def _fallback_universe(configured_backends: list[str], config: HarnessConfig) -> list[ModelCapability]:
    ordered = _preferred_backend_order(configured_backends, config)
    return [_FALLBACK_MODELS_BY_BACKEND[backend] for backend in ordered if backend in _FALLBACK_MODELS_BY_BACKEND]


def _preferred_backend_order(configured_backends: list[str], config: HarnessConfig) -> list[str]:
    if config.llm_backend == "auto":
        if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
            preference = ("accruvia_client", "command", "codex", "claude")
        else:
            preference = ("codex", "claude", "accruvia_client", "command")
    else:
        preference = (config.llm_backend, "codex", "claude", "accruvia_client", "command")
    return [backend for backend in preference if backend in configured_backends]
