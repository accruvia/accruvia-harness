"""Model discovery module (Step 2).

Probes each configured LLM backend to discover which models it can serve.
Results are converted into ``ModelCapability`` records and cached to a local
JSON file so that subsequent startups are cheap and deterministic.

Design rules:
- Discovery is explicit per backend — never guess from command strings.
- Probe commands are supplied via env/config, not hardcoded.
- If a probe fails the backend is still usable; it just reports
  ``available=True, probe_error=<message>`` with no detailed capabilities.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

from routellect.protocols import ModelCapability

logger = logging.getLogger(__name__)

# Env vars that supply per-backend probe commands.
_PROBE_ENV = {
    "codex": "ACCRUVIA_LLM_CODEX_MODELS_COMMAND",
    "claude": "ACCRUVIA_LLM_CLAUDE_MODELS_COMMAND",
    "accruvia_client": "ACCRUVIA_LLM_ACCRUVIA_CLIENT_MODELS_COMMAND",
    "command": "ACCRUVIA_LLM_COMMAND_MODELS_COMMAND",
}

_PROBE_TIMEOUT_SECONDS = 15

# Backend → provider mapping for cases where the probe doesn't report it.
_DEFAULT_PROVIDERS: dict[str, str] = {
    "codex": "openai",
    "claude": "anthropic",
    "accruvia_client": "anthropic",
    "command": "unknown",
}


def _run_probe(command: str) -> list[dict]:
    """Run a probe command and return parsed JSON list of model dicts."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Probe command timed out after {_PROBE_TIMEOUT_SECONDS}s: {command}")

    if result.returncode != 0:
        raise RuntimeError(
            f"Probe command failed (rc={result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )

    raw = result.stdout.strip()
    if not raw:
        raise RuntimeError("Probe command returned empty output")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Probe command returned invalid JSON: {exc}") from exc

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and "models" in payload:
        return payload["models"]
    raise RuntimeError(f"Probe command returned unexpected shape: {type(payload).__name__}")


def _parse_probe_result(backend: str, raw_models: list[dict]) -> list[ModelCapability]:
    """Convert raw probe dicts into ModelCapability records."""
    default_provider = _DEFAULT_PROVIDERS.get(backend, "unknown")
    capabilities: list[ModelCapability] = []
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("model_id") or entry.get("id") or entry.get("model")
        if not model_id:
            continue
        capabilities.append(
            ModelCapability(
                backend=backend,
                provider=str(entry.get("provider", default_provider)),
                model_id=str(model_id),
                supports_streaming=bool(entry.get("supports_streaming", False)),
                supports_tools=bool(entry.get("supports_tools", False)),
                max_context_tokens=entry.get("max_context_tokens"),
                available=True,
                probe_error=None,
            )
        )
    return capabilities


def probe_backend(backend: str) -> list[ModelCapability]:
    """Probe a single backend and return discovered models.

    Returns a single-element fallback list with ``probe_error`` set if the
    probe fails or no probe command is configured.
    """
    env_key = _PROBE_ENV.get(backend)
    command = os.environ.get(env_key, "") if env_key else ""
    if not command:
        return [
            ModelCapability(
                backend=backend,
                provider=_DEFAULT_PROVIDERS.get(backend, "unknown"),
                model_id="default",
                available=True,
                probe_error="no probe command configured",
            )
        ]
    try:
        raw = _run_probe(command)
        models = _parse_probe_result(backend, raw)
        if not models:
            raise RuntimeError("Probe returned zero valid model entries")
        return models
    except RuntimeError as exc:
        logger.warning("Model probe for %s failed: %s", backend, exc)
        return [
            ModelCapability(
                backend=backend,
                provider=_DEFAULT_PROVIDERS.get(backend, "unknown"),
                model_id="default",
                available=True,
                probe_error=str(exc),
            )
        ]


def discover_available_models(
    configured_backends: list[str],
) -> list[ModelCapability]:
    """Probe all configured backends and return the combined universe."""
    universe: list[ModelCapability] = []
    for backend in configured_backends:
        universe.extend(probe_backend(backend))
    return universe


def save_universe_cache(models: list[ModelCapability], cache_path: Path) -> None:
    """Persist discovered universe to a JSON file."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps([asdict(m) for m in models], indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_universe_cache(cache_path: Path) -> list[ModelCapability] | None:
    """Load a previously cached universe, or None if unavailable."""
    if not cache_path.exists():
        return None
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return None
        return [
            ModelCapability(
                backend=entry.get("backend", "unknown"),
                provider=entry.get("provider", "unknown"),
                model_id=entry.get("model_id", "unknown"),
                supports_streaming=bool(entry.get("supports_streaming", False)),
                supports_tools=bool(entry.get("supports_tools", False)),
                max_context_tokens=entry.get("max_context_tokens"),
                available=bool(entry.get("available", True)),
                probe_error=entry.get("probe_error"),
            )
            for entry in raw
            if isinstance(entry, dict)
        ]
    except (OSError, json.JSONDecodeError):
        return None
