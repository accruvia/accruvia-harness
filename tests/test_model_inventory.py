"""Tests for model_inventory (Step 2) and routing_hook (Steps 3, 6, 7)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from routellect.protocols import ModelCapability, RoutingDecision, RoutingOutcome
from routellect.routing_events import ModelUniverseSnapshot

from accruvia_harness.model_inventory import (
    _parse_probe_result,
    discover_available_models,
    load_universe_cache,
    probe_backend,
    save_universe_cache,
)
from accruvia_harness.exploration_policy import EpsilonGreedyPolicy
from accruvia_harness.routing_hook import RoutingHook


# ---------------------------------------------------------------------------
# Discovery parser per backend (Step 8 — unit tests)
# ---------------------------------------------------------------------------


class TestParseProbeResult:
    def test_parses_list_of_model_dicts(self):
        raw = [
            {"model_id": "claude-sonnet-4-6", "provider": "anthropic", "supports_tools": True},
            {"model_id": "claude-haiku-4-5", "provider": "anthropic"},
        ]
        result = _parse_probe_result("claude", raw)
        assert len(result) == 2
        assert result[0].model_id == "claude-sonnet-4-6"
        assert result[0].backend == "claude"
        assert result[0].supports_tools is True
        assert result[1].model_id == "claude-haiku-4-5"

    def test_skips_entries_without_model_id(self):
        raw = [{"provider": "openai"}, {"model_id": "gpt-4o"}]
        result = _parse_probe_result("codex", raw)
        assert len(result) == 1
        assert result[0].model_id == "gpt-4o"

    def test_uses_default_provider_when_absent(self):
        raw = [{"model_id": "gpt-4o"}]
        result = _parse_probe_result("codex", raw)
        assert result[0].provider == "openai"

    def test_accepts_id_and_model_keys(self):
        for key in ("model_id", "id", "model"):
            raw = [{key: "test-model"}]
            result = _parse_probe_result("command", raw)
            assert result[0].model_id == "test-model"


class TestProbeBackend:
    def test_returns_fallback_when_no_probe_command(self, monkeypatch):
        monkeypatch.delenv("ACCRUVIA_LLM_CODEX_MODELS_COMMAND", raising=False)
        result = probe_backend("codex")
        assert len(result) == 1
        assert result[0].model_id == "default"
        assert result[0].probe_error == "no probe command configured"
        assert result[0].available is True

    def test_returns_models_on_successful_probe(self, monkeypatch):
        monkeypatch.setenv("ACCRUVIA_LLM_CLAUDE_MODELS_COMMAND", "echo '[{\"model_id\":\"sonnet\"}]'")
        completed = subprocess.CompletedProcess(
            args="probe", returncode=0,
            stdout='[{"model_id": "sonnet", "provider": "anthropic"}]',
            stderr="",
        )
        with patch("accruvia_harness.model_inventory.subprocess.run", return_value=completed):
            result = probe_backend("claude")
        assert len(result) == 1
        assert result[0].model_id == "sonnet"
        assert result[0].probe_error is None

    def test_returns_fallback_on_probe_failure(self, monkeypatch):
        monkeypatch.setenv("ACCRUVIA_LLM_CODEX_MODELS_COMMAND", "false")
        completed = subprocess.CompletedProcess(
            args="false", returncode=1, stdout="", stderr="error",
        )
        with patch("accruvia_harness.model_inventory.subprocess.run", return_value=completed):
            result = probe_backend("codex")
        assert len(result) == 1
        assert result[0].probe_error is not None
        assert result[0].available is True


class TestDiscoverAvailableModels:
    def test_combines_multiple_backends(self, monkeypatch):
        monkeypatch.delenv("ACCRUVIA_LLM_CODEX_MODELS_COMMAND", raising=False)
        monkeypatch.delenv("ACCRUVIA_LLM_CLAUDE_MODELS_COMMAND", raising=False)
        result = discover_available_models(["codex", "claude"])
        assert len(result) == 2
        backends = {m.backend for m in result}
        assert backends == {"codex", "claude"}


# ---------------------------------------------------------------------------
# Universe normalization / cache (Step 8 — unit tests)
# ---------------------------------------------------------------------------


class TestUniverseCache:
    def test_save_and_load_roundtrip(self, tmp_path: Path):
        models = [
            ModelCapability(backend="claude", provider="anthropic", model_id="sonnet", available=True),
            ModelCapability(backend="codex", provider="openai", model_id="gpt-4o", available=True, supports_tools=True),
        ]
        cache_path = tmp_path / "cache.json"
        save_universe_cache(models, cache_path)
        loaded = load_universe_cache(cache_path)
        assert loaded is not None
        assert len(loaded) == 2
        assert loaded[0].model_id == "sonnet"
        assert loaded[1].supports_tools is True

    def test_load_returns_none_when_missing(self, tmp_path: Path):
        assert load_universe_cache(tmp_path / "nonexistent.json") is None

    def test_load_returns_none_on_corrupt_json(self, tmp_path: Path):
        cache_path = tmp_path / "bad.json"
        cache_path.write_text("not json", encoding="utf-8")
        assert load_universe_cache(cache_path) is None


# ---------------------------------------------------------------------------
# Exploration policy (Step 4) — unit tests with fixed random seed
# ---------------------------------------------------------------------------


class TestEpsilonGreedyPolicy:
    def _make_universe(self) -> ModelUniverseSnapshot:
        return ModelUniverseSnapshot(
            models=[
                ModelCapability(backend="claude", provider="anthropic", model_id="sonnet", available=True),
                ModelCapability(backend="codex", provider="openai", model_id="gpt-4o", available=True),
            ]
        )

    def test_exploit_selects_best_known_model(self):
        import random
        policy = EpsilonGreedyPolicy(epsilon=0.0, rng=random.Random(42))
        universe = self._make_universe()
        prior = [
            {"model_id": "sonnet", "success": True},
            {"model_id": "sonnet", "success": True},
            {"model_id": "gpt-4o", "success": False},
        ]
        decision = policy.select({"task": "test"}, universe, prior_outcomes=prior)
        assert decision.routing_decision.model_id == "sonnet"
        assert decision.exploration_flag is False

    def test_exploit_uses_metric_history_to_break_ties(self):
        import random
        policy = EpsilonGreedyPolicy(epsilon=0.0, rng=random.Random(42))
        universe = self._make_universe()
        prior = [
            {
                "model_id": "sonnet",
                "success": True,
                "llm_cost_usd": 1.5,
                "llm_total_tokens": 2000,
                "llm_latency_ms": 1100,
            },
            {
                "model_id": "gpt-4o",
                "success": True,
                "llm_cost_usd": 0.2,
                "llm_total_tokens": 300,
                "llm_latency_ms": 150,
            },
        ]
        decision = policy.select({"task": "test"}, universe, prior_outcomes=prior)
        assert decision.routing_decision.model_id == "gpt-4o"
        assert decision.exploration_flag is False

    def test_explore_selects_random_model(self):
        import random
        policy = EpsilonGreedyPolicy(epsilon=1.0, rng=random.Random(42))
        universe = self._make_universe()
        decision = policy.select({"task": "test"}, universe)
        assert decision.exploration_flag is True
        assert decision.routing_decision.is_exploration is True

    def test_high_risk_profile_disables_exploration(self):
        import random
        policy = EpsilonGreedyPolicy(
            epsilon=1.0,
            rng=random.Random(42),
            high_risk_profiles={"critical"},
        )
        universe = self._make_universe()
        decision = policy.select({"task": "test"}, universe, validation_profile="critical")
        assert decision.exploration_flag is False

    def test_raises_on_empty_universe(self):
        import random
        policy = EpsilonGreedyPolicy(epsilon=0.0, rng=random.Random(42))
        universe = ModelUniverseSnapshot(models=[])
        with pytest.raises(ValueError, match="No available models"):
            policy.select({"task": "test"}, universe)


# ---------------------------------------------------------------------------
# Routing hook — safety rails and integration (Steps 3, 6, 7)
# ---------------------------------------------------------------------------


class TestRoutingHook:
    def _make_hook(self, epsilon: float = 0.0, cache_path: Path | None = None) -> RoutingHook:
        import random
        models = [
            ModelCapability(backend="claude", provider="anthropic", model_id="sonnet", available=True),
            ModelCapability(backend="codex", provider="openai", model_id="gpt-4o", available=True),
        ]
        universe = ModelUniverseSnapshot(models=models)
        policy = EpsilonGreedyPolicy(epsilon=epsilon, rng=random.Random(42))
        return RoutingHook(universe=universe, policy=policy, cache_path=cache_path)

    def test_select_model_returns_valid_decision(self):
        hook = self._make_hook()
        decision = hook.select_model_for({"task": "test"})
        assert decision.routing_decision.model_id in ("sonnet", "gpt-4o")
        assert decision.universe_snapshot_id == hook.universe.snapshot_id

    def test_decision_stamped_with_universe_hash(self):
        hook = self._make_hook()
        decision = hook.select_model_for({"task": "test"})
        assert decision.routing_decision.universe_hash == hook.universe.snapshot_id
        assert len(decision.routing_decision.universe_hash) == 16

    def test_event_log_records_decision(self):
        hook = self._make_hook()
        hook.select_model_for({"task": "test"})
        log = hook.get_event_log()
        assert len(log) == 1
        assert "decision" in log[0]

    def test_record_outcome_adds_to_event_log(self):
        hook = self._make_hook()
        decision = RoutingDecision(
            model_id="sonnet", backend="claude", confidence=1.0,
            reasoning="best fit", universe_hash=hook.universe.snapshot_id,
            is_exploration=False,
        )
        outcome = RoutingOutcome(success=True, latency_ms=100)
        hook.record_outcome(decision, outcome, token_metrics={"llm_total_tokens": 42.0})
        log = hook.get_event_log()
        assert len(log) == 1
        assert log[0]["outcome"]["success"] is True
        assert log[0]["token_metrics"]["llm_total_tokens"] == 42.0

    def test_select_uses_prior_outcomes_for_exploit(self):
        hook = self._make_hook()
        decision = RoutingDecision(
            model_id="gpt-4o",
            backend="codex",
            confidence=1.0,
            reasoning="best fit",
            universe_hash=hook.universe.snapshot_id,
            is_exploration=False,
        )
        outcome = RoutingOutcome(
            success=True,
            latency_ms=120,
            cost_usd=0.1,
            token_count=120,
        )
        hook.record_outcome(decision, outcome)

        prior = [{"model_id": "gpt-4o", "success": True}]
        selected = hook.select_model_for({"task": "test"}, prior_outcomes=prior)
        assert selected.routing_decision.model_id == "gpt-4o"

    def test_outcome_history_persists_via_event_log(self, tmp_path: Path):
        hook = self._make_hook()
        decision = RoutingDecision(
            model_id="gpt-4o",
            backend="codex",
            confidence=1.0,
            reasoning="best fit",
            universe_hash=hook.universe.snapshot_id,
            is_exploration=False,
        )
        outcome = RoutingOutcome(
            success=True,
            latency_ms=90,
            cost_usd=0.05,
            token_count=75,
        )
        hook.record_outcome(decision, outcome)

        log = hook.get_event_log()
        assert len(log) == 1
        # Use recorded event log as prior_outcomes for next selection
        prior = [{"model_id": "gpt-4o", "success": True}]
        selected = hook.select_model_for({"task": "test"}, prior_outcomes=prior)
        assert selected.routing_decision.model_id == "gpt-4o"

    def test_fallback_on_unknown_model(self):
        """Safety rail: if policy somehow returns a model not in the
        universe, the hook should fall back to a known model."""
        import random

        models = [
            ModelCapability(backend="claude", provider="anthropic", model_id="sonnet", available=True),
        ]
        universe = ModelUniverseSnapshot(models=models)

        class BadPolicy(EpsilonGreedyPolicy):
            def select(self, task_fingerprint, universe, prior_outcomes=None, validation_profile="generic"):
                from accruvia_harness.exploration_policy import PolicyDecision
                return PolicyDecision(
                    routing_decision=RoutingDecision(
                        model_id="nonexistent-model",
                        backend="fake",
                        confidence=1.0,
                        reasoning="bad policy",
                        universe_hash=universe.snapshot_id,
                        is_exploration=False,
                    ),
                    exploration_flag=False,
                    rationale="bad policy",
                    universe_snapshot_id=universe.snapshot_id,
                    task_fingerprint=task_fingerprint,
                )

        hook = RoutingHook(
            universe=universe,
            policy=BadPolicy(rng=random.Random(42)),
        )
        decision = hook.select_model_for({"task": "test"})
        assert decision.routing_decision.model_id == "sonnet"
        assert "fallback" in decision.routing_decision.reasoning

    def test_no_discovery_required_for_local_only(self, monkeypatch):
        """Regression: existing auto backend still works when no probes are
        configured — discovery returns fallback entries."""
        for key in (
            "ACCRUVIA_LLM_CODEX_MODELS_COMMAND",
            "ACCRUVIA_LLM_CLAUDE_MODELS_COMMAND",
            "ACCRUVIA_LLM_ACCRUVIA_CLIENT_MODELS_COMMAND",
            "ACCRUVIA_LLM_COMMAND_MODELS_COMMAND",
        ):
            monkeypatch.delenv(key, raising=False)
        models = discover_available_models(["claude"])
        assert len(models) >= 1
        assert all(m.available for m in models)
