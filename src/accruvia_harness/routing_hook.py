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

import json
import logging
from pathlib import Path
import sqlite3
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

# Safe fallback when discovery fails entirely.
_SAFE_DEFAULT_UNIVERSE = [
    ModelCapability(
        backend="claude",
        provider="anthropic",
        model_id="claude-sonnet-4-6",
        supports_streaming=True,
        supports_tools=True,
        available=True,
    ),
]


class RoutingHook:
    """Single integration point between harness and routellect routing."""

    def __init__(
        self,
        universe: ModelUniverseSnapshot,
        policy: EpsilonGreedyPolicy,
        cache_path: Path | None = None,
        db_path: Path | None = None,
    ) -> None:
        self.universe = universe
        self.policy = policy
        self.cache_path = cache_path
        self.db_path = Path(db_path) if db_path is not None else None
        self._event_log: list[dict[str, Any]] = []
        self._outcome_history: list[dict[str, Any]] = []
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_outcome_history_table()
            self._outcome_history = self._load_outcome_history()

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
                logger.warning("All probes failed and no cache — using safe default universe")
                models = list(_SAFE_DEFAULT_UNIVERSE)

        universe = ModelUniverseSnapshot(models=models)
        save_universe_cache(models, cache_path)

        policy = EpsilonGreedyPolicy(
            epsilon=epsilon,
            high_risk_profiles=high_risk_profiles or set(),
        )

        return cls(
            universe=universe,
            policy=policy,
            cache_path=cache_path,
            db_path=config.db_path,
        )

    def select_model_for(
        self,
        task_fingerprint: dict[str, Any],
        validation_profile: str = "generic",
        prior_outcomes: list[dict[str, Any]] | None = None,
    ) -> PolicyDecision:
        """Select a model for an invocation, with safety checks."""
        effective_prior_outcomes = self._outcome_history if prior_outcomes is None else prior_outcomes
        decision = self.policy.select(
            task_fingerprint=task_fingerprint,
            universe=self.universe,
            prior_outcomes=effective_prior_outcomes,
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
    ) -> None:
        """Record an invocation outcome for learning and audit."""
        event = RoutingOutcomeEvent(
            decision=decision,
            outcome=outcome,
            universe_snapshot_id=self.universe.snapshot_id,
        )
        outcome_record = {
            "model_id": decision.model_id,
            "backend": decision.backend,
            "success": bool(outcome.success),
            "llm_cost_usd": float(outcome.extra.get("llm_cost_usd", outcome.cost)),
            "llm_total_tokens": float(
                outcome.extra.get("llm_total_tokens", outcome.input_tokens + outcome.output_tokens)
            ),
            "llm_latency_ms": float(outcome.extra.get("llm_latency_ms", outcome.latency_ms)),
        }
        self._outcome_history.append(outcome_record)
        self._persist_outcome(outcome_record)
        self._event_log.append(event.to_dict())

    def get_event_log(self) -> list[dict[str, Any]]:
        """Return all routing events emitted during this session."""
        return list(self._event_log)

    def _connect(self) -> sqlite3.Connection:
        if self.db_path is None:
            raise RuntimeError("RoutingHook persistence requested without db_path")
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _ensure_outcome_history_table(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS routing_outcome_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    model_id TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    llm_cost_usd REAL NOT NULL DEFAULT 0.0,
                    llm_total_tokens REAL NOT NULL DEFAULT 0.0,
                    llm_latency_ms REAL NOT NULL DEFAULT 0.0,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_routing_outcome_history_recorded_at
                ON routing_outcome_history(recorded_at)
                """
            )

    def _load_outcome_history(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, model_id, success, llm_cost_usd, llm_total_tokens, llm_latency_ms
                FROM routing_outcome_history
                ORDER BY id ASC
                """
            ).fetchall()
        loaded: list[dict[str, Any]] = []
        for row in rows:
            payload: dict[str, Any] | None = None
            raw = row["payload_json"]
            if isinstance(raw, str) and raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    payload = parsed
            if payload is None:
                payload = {
                    "model_id": row["model_id"],
                    "success": bool(row["success"]),
                    "llm_cost_usd": float(row["llm_cost_usd"]),
                    "llm_total_tokens": float(row["llm_total_tokens"]),
                    "llm_latency_ms": float(row["llm_latency_ms"]),
                }
            loaded.append(payload)
        return loaded

    def _persist_outcome(self, outcome_record: dict[str, Any]) -> None:
        if self.db_path is None:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO routing_outcome_history (
                    model_id, success, llm_cost_usd, llm_total_tokens, llm_latency_ms, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(outcome_record.get("model_id", "")),
                    1 if outcome_record.get("success") else 0,
                    float(outcome_record.get("llm_cost_usd", 0.0) or 0.0),
                    float(outcome_record.get("llm_total_tokens", 0.0) or 0.0),
                    float(outcome_record.get("llm_latency_ms", 0.0) or 0.0),
                    json.dumps(outcome_record, sort_keys=True),
                ),
            )


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
