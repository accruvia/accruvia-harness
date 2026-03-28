"""Routing event definitions for routellect - stub implementation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .protocols import ModelCapability, RoutingDecision, RoutingOutcome


@dataclass
class ModelUniverseSnapshot:
    """Snapshot of available models at a point in time."""
    models: list[ModelCapability]
    
    @property
    def snapshot_id(self) -> str:
        """Generate a stable hash for this universe snapshot."""
        model_data = sorted([
            (m.model_id, m.backend, m.available) 
            for m in self.models
        ])
        content = json.dumps(model_data, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


@dataclass
class RoutingDecisionEvent:
    """Event capturing a routing decision."""
    task_fingerprint: dict[str, Any]
    decision: RoutingDecision
    universe_snapshot_id: str
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)


@dataclass 
class RoutingOutcomeEvent:
    """Event capturing the outcome of a routing decision."""
    decision: RoutingDecision
    outcome: RoutingOutcome
    universe_snapshot_id: str
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)