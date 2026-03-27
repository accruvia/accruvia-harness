"""
Tests proving routing_outcome_history is populated by LLMTaskWorker.

Covers:
  - routing_hook is wired through build_worker_from_config
  - success outcomes record real token metrics in SQLite
  - failure outcomes record success=0 in SQLite
  - outcome history survives a RoutingHook restart (SQLite persistence)
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import Run, RunStatus, Task
from accruvia_harness.llm import LLMExecutionError, LLMExecutionResult
from accruvia_harness.routing_hook import RoutingHook
from accruvia_harness.workers import LLMTaskWorker, build_worker_from_config


# ── helpers ────────────────────────────────────────────────────────────────

def _make_config(base: Path) -> HarnessConfig:
    return HarnessConfig(
        db_path=base / "harness.db",
        workspace_root=base / "workspace",
        log_path=base / "harness.log",
        telemetry_dir=base / "telemetry",
        default_project_name="feedback-test",
        default_repo="test/repo",
        runtime_backend="local",
        temporal_target="localhost:7233",
        temporal_namespace="default",
        temporal_task_queue="accruvia-harness",
        worker_backend="llm",
        worker_command=None,
        llm_backend="codex",
        llm_model="gpt-5.3-codex",
        llm_command=None,
        llm_codex_command="codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox",
        llm_claude_command=None,
        llm_accruvia_client_command=None,
    )


def _make_task() -> Task:
    return Task(
        id="test_task",
        project_id="test_project",
        title="Test task",
        objective="Write a hello world program",
    )


def _make_run() -> Run:
    return Run(
        id="test_run",
        task_id="test_task",
        status=RunStatus.WORKING,
        attempt=1,
        summary="",
    )


def _make_hook(db_path: Path) -> RoutingHook:
    from routellect.protocols import ModelCapability
    from routellect.routing_events import ModelUniverseSnapshot
    from accruvia_harness.exploration_policy import EpsilonGreedyPolicy

    model = ModelCapability(
        backend="codex",
        provider="openai",
        model_id="gpt-5.3-codex",
        supports_streaming=False,
        supports_tools=False,
        available=True,
    )
    universe = ModelUniverseSnapshot(models=[model])
    policy = EpsilonGreedyPolicy(epsilon=0.0)
    return RoutingHook(universe=universe, policy=policy, db_path=db_path)


def _make_success_result(run_dir: Path) -> LLMExecutionResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = run_dir / "llm_prompt.txt"
    response_path = run_dir / "llm_response.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    response_path.write_text("response", encoding="utf-8")
    return LLMExecutionResult(
        backend="codex",
        response_text="done",
        prompt_path=prompt_path,
        response_path=response_path,
        diagnostics={
            "llm_cost_usd": 0.0042,
            "llm_prompt_tokens": 100,
            "llm_completion_tokens": 50,
            "llm_total_tokens": 150,
            "llm_latency_ms": 1234.0,
        },
    )


# ── tests ──────────────────────────────────────────────────────────────────

def test_build_worker_from_config_passes_routing_hook():
    """build_worker_from_config must pass routing_hook through to LLMTaskWorker."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        config = _make_config(base)
        hook = _make_hook(config.db_path)

        captured = {}
        original_init = LLMTaskWorker.__init__

        def capturing_init(self, router, model=None, telemetry=None, routing_hook=None):
            captured["routing_hook"] = routing_hook
            original_init(self, router, model=model, telemetry=telemetry, routing_hook=routing_hook)

        with patch("accruvia_harness.workers.LLMTaskWorker.__init__", capturing_init):
            build_worker_from_config(config, routing_hook=hook)

        assert captured.get("routing_hook") is hook, (
            "routing_hook was not threaded through build_worker_from_config into LLMTaskWorker"
        )


def test_success_outcome_populates_sqlite():
    """After a successful task run, routing_outcome_history must have a row with real token metrics."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        config = _make_config(base)
        hook = _make_hook(config.db_path)
        workspace = config.workspace_root
        workspace.mkdir(parents=True, exist_ok=True)

        task = _make_task()
        run = _make_run()
        success_result = _make_success_result(workspace / "runs" / run.id)

        mock_router = MagicMock()
        mock_router.backend = "codex"
        mock_router.execute.return_value = (success_result, "codex")

        worker = LLMTaskWorker(router=mock_router, model="gpt-5.3-codex", routing_hook=hook)
        result = worker.work(task, run, workspace)

        assert result.outcome == "success"

        conn = sqlite3.connect(config.db_path)
        rows = conn.execute(
            "SELECT model_id, success, llm_cost_usd, llm_total_tokens, llm_latency_ms "
            "FROM routing_outcome_history"
        ).fetchall()
        conn.close()

        assert len(rows) == 1, f"Expected 1 row in routing_outcome_history, got {len(rows)}"
        model_id, success, cost, tokens, latency = rows[0]
        assert success == 1, "success column must be 1 for a successful run"
        assert abs(cost - 0.0042) < 1e-5, f"cost mismatch: {cost}"
        assert abs(tokens - 150.0) < 1e-5, f"total_tokens mismatch: {tokens}"
        assert abs(latency - 1234.0) < 1e-5, f"latency_ms mismatch: {latency}"


def test_failure_outcome_populates_sqlite():
    """After an LLMExecutionError, routing_outcome_history must record success=0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        config = _make_config(base)
        hook = _make_hook(config.db_path)
        workspace = config.workspace_root
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "runs" / "test_run").mkdir(parents=True, exist_ok=True)

        task = _make_task()
        run = _make_run()

        mock_router = MagicMock()
        mock_router.backend = "codex"
        mock_router.execute.side_effect = LLMExecutionError("codex: rate limit exceeded")

        worker = LLMTaskWorker(router=mock_router, model="gpt-5.3-codex", routing_hook=hook)
        result = worker.work(task, run, workspace)

        assert result.outcome in ("failed", "blocked")

        conn = sqlite3.connect(config.db_path)
        rows = conn.execute(
            "SELECT model_id, success FROM routing_outcome_history"
        ).fetchall()
        conn.close()

        assert len(rows) == 1, f"Expected 1 failure row, got {len(rows)}"
        _, success = rows[0]
        assert success == 0, "Failure outcome must be recorded with success=0"


def test_outcome_history_survives_restart():
    """Rows written to SQLite must be loaded back into a fresh RoutingHook instance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        config = _make_config(base)
        workspace = config.workspace_root
        workspace.mkdir(parents=True, exist_ok=True)

        task = _make_task()
        run = _make_run()
        success_result = _make_success_result(workspace / "runs" / run.id)

        hook1 = _make_hook(config.db_path)
        mock_router = MagicMock()
        mock_router.backend = "codex"
        mock_router.execute.return_value = (success_result, "codex")

        worker = LLMTaskWorker(router=mock_router, model="gpt-5.3-codex", routing_hook=hook1)
        worker.work(task, run, workspace)

        # Simulate restart: fresh RoutingHook pointed at the same DB.
        hook2 = _make_hook(config.db_path)
        assert len(hook2._outcome_history) == 1, (
            f"Expected 1 outcome loaded after restart, got {len(hook2._outcome_history)}"
        )
        assert hook2._outcome_history[0]["success"] is True, (
            "Loaded outcome must have success=True"
        )
