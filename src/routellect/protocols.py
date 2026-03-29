"""Protocol definitions for routellect - stub implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ModelCapability:
    """Describes a model's capabilities and availability."""
    backend: str
    provider: str
    model_id: str
    available: bool
    probe_error: str | None = None
    supports_streaming: bool = True
    supports_tools: bool = True
    max_context_tokens: int | None = None


@dataclass
class RoutingDecision:
    """A routing decision for model selection."""
    model_id: str
    backend: str
    confidence: float
    reasoning: str
    universe_hash: str
    is_exploration: bool


@dataclass
class RoutingOutcome:
    """The outcome of a routing decision."""
    success: bool
    latency_ms: float
    token_count: int | None = None
    cost_usd: float | None = None
    error_type: str | None = None
    error_message: str | None = None