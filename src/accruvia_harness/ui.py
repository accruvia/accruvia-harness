from __future__ import annotations

import json
import errno
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import datetime as _dt
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from queue import Queue, Empty

from .commands.common import resolve_project_ref

def _get_git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"

_GIT_COMMIT = _get_git_commit()
_SERVER_STARTED_AT = _dt.datetime.now(_dt.timezone.utc).isoformat()
from .context_control import objective_execution_gate
from .frustration_triage import triage_frustration
from .llm import LLMExecutionError, LLMInvocation
from .services.task_service import TaskService
from .ui_memory import LocalContextMemoryProvider
from .ui_responder import (
    ConversationTurn,
    ObjectiveResponderContext,
    ResponderResult,
    ResponderContextPacket,
    RunResponderContext,
    TaskResponderContext,
    answer_ui_message,
)
from .domain import (
    ContextRecord,
    IntentModel,
    MermaidArtifact,
    MermaidStatus,
    Objective,
    ObjectiveStatus,
    PromotionMode,
    PromotionStatus,
    RepoProvider,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    new_id,
    serialize_dataclass,
)


class AtomicGenerationCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def start(self, objective_id: str, worker) -> bool:
        with self._lock:
            if objective_id in self._running:
                return False
            self._running.add(objective_id)
        thread = threading.Thread(target=self._run, args=(objective_id, worker), daemon=True)
        thread.start()
        return True

    def _run(self, objective_id: str, worker) -> None:
        try:
            worker()
        finally:
            with self._lock:
                self._running.discard(objective_id)


_ATOMIC_GENERATION = AtomicGenerationCoordinator()


class ObjectiveReviewCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def start(self, objective_id: str, worker) -> bool:
        with self._lock:
            if objective_id in self._running:
                return False
            self._running.add(objective_id)
        thread = threading.Thread(target=self._run, args=(objective_id, worker), daemon=True)
        thread.start()
        return True

    def _run(self, objective_id: str, worker) -> None:
        try:
            worker()
        finally:
            with self._lock:
                self._running.discard(objective_id)


_OBJECTIVE_REVIEW = ObjectiveReviewCoordinator()

_OBJECTIVE_REVIEW_DIMENSIONS = frozenset(
    {
        "intent_fidelity",
        "unit_test_coverage",
        "integration_e2e_coverage",
        "security",
        "devops",
        "atomic_fidelity",
        "code_structure",
    }
)
_OBJECTIVE_REVIEW_VERDICTS = frozenset({"pass", "concern", "remediation_required"})
_OBJECTIVE_REVIEW_PROGRESS = frozenset(
    {"new_concern", "still_blocking", "improving", "resolved", "not_applicable"}
)
_OBJECTIVE_REVIEW_SEVERITIES = frozenset({"low", "medium", "high"})
_OBJECTIVE_REVIEW_REBUTTAL_OUTCOMES = frozenset(
    {"accepted", "wrong_artifact_type", "artifact_incomplete", "missing_terminal_event", "evidence_not_found"}
)
_OBJECTIVE_REVIEW_VAGUE_PHRASES = (
    "improve",
    "better",
    "more coverage",
    "additional tests",
    "stronger evidence",
    "more evidence",
    "further validation",
    "review further",
    "be reviewed",
)


class BackgroundSupervisorCoordinator:
    """Manages background supervisor threads, one per project."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, threading.Event] = {}  # project_id -> stop event
        self._status: dict[str, dict[str, object]] = {}  # project_id -> latest status

    def start(self, project_id: str, engine, *, watch: bool = True) -> bool:
        with self._lock:
            if project_id in self._running:
                return False
            stop_event = threading.Event()
            self._running[project_id] = stop_event
            self._status[project_id] = {
                "state": "starting",
                "processed_count": 0,
                "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }

        def worker() -> None:
            try:
                # Wire stop signal to the worker so it kills the subprocess on stop.
                if hasattr(engine.worker, "set_stop_requested"):
                    engine.worker.set_stop_requested(stop_event.is_set)
                self._status[project_id]["state"] = "running"
                result = engine.supervise(
                    project_id=project_id,
                    worker_id=f"ui-supervisor-{project_id[:8]}",
                    watch=watch,
                    idle_sleep_seconds=10.0,
                    max_idle_cycles=None,
                    stop_requested=stop_event.is_set,
                    progress_callback=lambda ev: self._on_progress(project_id, ev),
                )
                self._status[project_id].update({
                    "state": "finished",
                    "processed_count": result.processed_count,
                    "exit_reason": result.exit_reason,
                    "finished_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                })
            except Exception as exc:
                self._status[project_id].update({
                    "state": "error",
                    "error": str(exc),
                    "finished_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                })
            finally:
                if hasattr(engine.worker, "set_stop_requested"):
                    engine.worker.set_stop_requested(None)
                with self._lock:
                    self._running.pop(project_id, None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return True

    def stop(self, project_id: str) -> bool:
        with self._lock:
            stop_event = self._running.get(project_id)
            if stop_event is None:
                return False
            stop_event.set()
            status = self._status.get(project_id, {})
            status["state"] = "stopping"
            return True

    def is_running(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._running

    def status(self, project_id: str) -> dict[str, object]:
        return dict(self._status.get(project_id, {"state": "idle"}))

    def _on_progress(self, project_id: str, event: dict[str, object]) -> None:
        event_type = event.get("type", "")
        status = self._status.get(project_id, {})
        if event_type == "task_finished":
            status["processed_count"] = status.get("processed_count", 0) + 1
            status["last_task_id"] = event.get("task_id")
            status["last_task_title"] = event.get("task_title")
            status["last_task_status"] = event.get("status")
        status["last_event"] = event_type
        status["last_event_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()


_BACKGROUND_SUPERVISOR = BackgroundSupervisorCoordinator()


_APP_CSS = """
:root {
  color-scheme: light;
  --bg: #f5f1e8;
  --panel: #fffaf0;
  --ink: #1f2933;
  --muted: #6b7280;
  --line: #dfd3b8;
  --accent: #a24c2b;
  --accent-soft: #f3d9c9;
  --success: #2f6f4f;
}

* { box-sizing: border-box; }
button:disabled {
  cursor: wait;
  opacity: 0.55;
}
body {
  margin: 0;
  font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
  color: var(--ink);
  background:
    radial-gradient(circle at top left, #fff9ef 0, #f5f1e8 55%),
    linear-gradient(135deg, #efe2c4 0, #f5f1e8 100%);
}

.app-shell {
  display: grid;
  grid-template-columns: 280px 1fr;
  min-height: 100vh;
  transition: grid-template-columns 180ms ease;
}

body[data-layout="split-workspace"] .app-shell,
body[data-layout="full-review"] .app-shell {
  grid-template-columns: 1fr;
}

body[data-layout="dashboard"] .app-shell {
  display: block;
}

body[data-layout="split-workspace"] .content {
  display: grid;
  grid-template-columns: minmax(420px, 0.95fr) minmax(520px, 1.05fr);
  align-items: start;
  gap: 0;
  min-height: 100vh;
  padding: 0;
  background: #ffffff;
}

body[data-layout="full-review"] .content {
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  padding: 1rem;
}

body[data-layout="dashboard"] .content {
  max-width: 100%;
  width: 100%;
  margin: 0;
  padding: 0;
}

body[data-view="control-flow"] .sidebar,
body[data-view="control-flow"] .header,
body[data-view="control-flow"] #objective-panel,
body[data-view="control-flow"] #interrogation-panel,
body[data-view="control-flow"] #execution-panel,
body[data-view="control-flow"] #cli-panel,
body[data-view="control-flow"] #step-back,
body[data-view="control-flow"] #step-expand,
body[data-view="control-flow"] #next-action-saved,
body[data-view="control-flow"] #workspace-title,
body[data-view="control-flow"] #workspace-summary {
  display: none !important;
}

body[data-view="control-flow"] #next-action-panel {
  display: block !important;
  min-height: 100vh;
  border-radius: 0;
  border: none;
  box-shadow: none;
  margin: 0;
  padding: 1.25rem;
  background: #ffffff;
}

body[data-view="control-flow"] #content-grid {
  display: block;
  width: 100%;
  margin: 0;
}

body[data-view="control-flow"] #mermaid-panel {
  display: block !important;
  width: 100%;
  min-height: 100vh;
  max-width: none;
  border-radius: 0;
  border: none;
  box-shadow: none;
  margin: 0;
  padding: 0;
  background: #ffffff;
}

body[data-view="control-flow"] #mermaid-panel > h3,
body[data-view="control-flow"] #mermaid-step-prompt {
  display: none !important;
}

body[data-view="control-flow"] #diagram-shell {
  min-height: 100vh;
  border: none;
  border-radius: 0;
  margin: 0;
  padding: 2rem;
  background: #ffffff;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  overflow: auto;
  cursor: grab;
}

body[data-view="control-flow"] #diagram-shell svg {
  width: min(1100px, calc(100vw - 4rem));
  height: auto;
  max-width: 100%;
  display: block;
  user-select: none;
}

body[data-view="control-flow"] #objective-banner {
  display: grid !important;
  margin-bottom: 1rem;
  background: #ffffff;
}

body[data-view="control-flow"] .expectation-grid,
body[data-view="control-flow"] #next-action-title,
body[data-view="control-flow"] #next-action-body {
  display: none !important;
}

body[data-view="control-flow"] .conversation-transcript {
  min-height: 55vh;
  max-height: 55vh;
  background: #ffffff;
  color: var(--ink);
}

body[data-view="control-flow"] .conversation-form {
  display: grid !important;
  background: #ffffff;
}

body[data-view="control-flow"] #mermaid-controls {
  display: none !important;
}

body[data-view="atomic"] .sidebar,
body[data-view="atomic"] #objective-panel,
body[data-view="atomic"] #interrogation-panel,
body[data-view="atomic"] #execution-panel,
body[data-view="atomic"] #cli-panel,
body[data-view="atomic"] #step-back,
body[data-view="atomic"] #step-expand,
body[data-view="atomic"] #next-action-saved,
body[data-view="atomic"] #workspace-title,
body[data-view="atomic"] #workspace-summary,
body[data-view="atomic"] #mermaid-panel {
  display: none !important;
}

body[data-view="atomic"] .header {
  display: flex !important;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  grid-column: 1 / -1;
  padding: 0.9rem 1.25rem 0.25rem;
  border: none;
  background: #ffffff;
}

body[data-view="atomic"] #next-action-panel {
  display: block !important;
  grid-column: 1;
  grid-row: 2;
  min-height: 100vh;
  border-radius: 0;
  border: none;
  box-shadow: none;
  margin: 0;
  padding: 1.25rem;
  background: #ffffff;
}

body[data-view="atomic"] #content-grid {
  display: block;
  grid-column: 2;
  grid-row: 2;
  width: 100%;
  margin: 0;
}

body[data-view="atomic"] #supervisor-panel {
  display: block !important;
  width: 100%;
  max-width: none;
  border-radius: 0;
  border: none;
  border-bottom: 1px solid var(--line);
  box-shadow: none;
  margin: 0;
  padding: 1.25rem;
  background: #fffdf8;
}

body:not([data-view="atomic"]) #supervisor-panel {
  display: none !important;
}

/* === Harness dashboard view === */
body[data-view="harness"] .sidebar { display: none !important; }
body[data-view="harness"] .header { display: none !important; }
body[data-view="harness"] #next-action-panel { display: none !important; }
body[data-view="harness"] #objective-panel { display: none !important; }
body[data-view="harness"] #interrogation-panel { display: none !important; }
body[data-view="harness"] #mermaid-panel { display: none !important; }
body[data-view="harness"] #execution-panel { display: none !important; }
body[data-view="harness"] #supervisor-panel { display: none !important; }
body[data-view="harness"] #atomic-panel { display: none !important; }
body[data-view="harness"] #cli-panel { display: none !important; }
body[data-view="harness"] .grid { display: block; padding: 0; gap: 0; max-width: 100%; width: 100%; }

body[data-view="harness"] #harness-dashboard {
  display: grid !important;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: auto auto 1fr;
  gap: 1.25rem;
  width: 100%;
  max-width: 100%;
  border: none;
  border-radius: 0;
  box-shadow: none;
  margin: 0;
  padding: 1.5rem 2rem;
  background: var(--bg);
  min-height: 100vh;
}

body[data-view="harness"] #harness-dashboard .harness-global-status {
  grid-column: 1 / -1;
}

body[data-view="harness"] #harness-dashboard .harness-llm-health {
  grid-column: 1 / -1;
}

body[data-view="harness"] #harness-dashboard .harness-project-cards {
  grid-column: 1;
  grid-row: 3;
}

body[data-view="harness"] #harness-dashboard .harness-event-feed {
  grid-column: 2;
  grid-row: 3;
}

.harness-global-status {
  margin-bottom: 1.5rem;
}

.harness-global-status h2 {
  margin: 0 0 0.5rem 0;
  font-size: 1.3rem;
}

.harness-global-status .global-progress-bar {
  display: flex;
  height: 0.75rem;
  border-radius: 0.4rem;
  overflow: hidden;
  background: #e8e2d4;
  margin-bottom: 0.4rem;
}

.harness-global-status .global-progress-bar .segment { transition: width 0.3s ease; }
.harness-global-status .global-progress-bar .segment.completed { background: #2f6f4f; }
.harness-global-status .global-progress-bar .segment.active { background: #d9b26a; }
.harness-global-status .global-progress-bar .segment.failed { background: #c0504d; }
.harness-global-status .global-progress-bar .segment.pending { background: #dfd3b8; }

.harness-global-status .summary {
  font-size: 0.88rem;
  color: var(--muted);
}

.harness-llm-health {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 1.5rem;
  flex-wrap: wrap;
}

.harness-llm-health .llm-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.25rem 0.65rem;
  border-radius: 0.4rem;
  font-size: 0.82rem;
  font-weight: 600;
  border: 1px solid var(--line);
}

.harness-llm-health .llm-badge.healthy {
  background: #edf9f0;
  border-color: #8fc8a5;
  color: #1f6b35;
}

.harness-llm-health .llm-badge.demoted {
  background: #fff1f1;
  border-color: #d49b9b;
  color: #9f2f2f;
}

.harness-llm-health .llm-badge .dot {
  width: 0.5rem;
  height: 0.5rem;
  border-radius: 50%;
}

.harness-llm-health .llm-badge.healthy .dot { background: #2f6f4f; }
.harness-llm-health .llm-badge.demoted .dot { background: #c0504d; }

.harness-project-cards {
  display: grid;
  grid-template-columns: 1fr;
  gap: 0.75rem;
}

.harness-project-card {
  border: 1px solid var(--line);
  border-radius: 0.75rem;
  padding: 1rem;
  background: var(--panel);
}

.harness-project-card .project-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.5rem;
}

.harness-project-card .project-name {
  font-weight: 700;
  font-size: 1rem;
}

.harness-project-card .supervisor-pill {
  font-size: 0.72rem;
  padding: 0.1rem 0.45rem;
  border-radius: 0.3rem;
  font-weight: 600;
}

.harness-project-card .supervisor-pill.running { background: #fff5df; color: #8b5a00; }
.harness-project-card .supervisor-pill.idle { background: #f0ebe0; color: #7a7060; }
.harness-project-card .supervisor-pill.finished { background: #edf9f0; color: #1f6b35; }
.harness-project-card .supervisor-pill.error { background: #fff1f1; color: #9f2f2f; }

.harness-project-card .mini-progress {
  display: flex;
  height: 0.4rem;
  border-radius: 0.2rem;
  overflow: hidden;
  background: #e8e2d4;
  margin-bottom: 0.35rem;
}

.harness-project-card .mini-progress .segment { transition: width 0.3s ease; }
.harness-project-card .mini-progress .segment.completed { background: #2f6f4f; }
.harness-project-card .mini-progress .segment.active { background: #d9b26a; }
.harness-project-card .mini-progress .segment.failed { background: #c0504d; }
.harness-project-card .mini-progress .segment.pending { background: #dfd3b8; }

.harness-project-card .task-summary {
  font-size: 0.82rem;
  color: var(--muted);
  margin-bottom: 0.35rem;
}

.harness-project-card .objective-name {
  font-size: 0.88rem;
  color: var(--ink);
}

.harness-event-feed h3 {
  margin: 0 0 0.75rem 0;
  font-size: 1.1rem;
}

.harness-feed-list {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  max-height: 50vh;
  overflow-y: auto;
}

.harness-feed-item {
  display: flex;
  gap: 0.75rem;
  padding: 0.4rem 0.6rem;
  border-radius: 0.4rem;
  font-size: 0.82rem;
  background: var(--panel);
  border: 1px solid transparent;
}

.harness-feed-item:hover {
  border-color: var(--line);
}

.harness-feed-item .feed-time {
  color: var(--muted);
  flex-shrink: 0;
  min-width: 5.5rem;
}

.harness-feed-item .feed-project {
  color: var(--accent);
  font-weight: 600;
  flex-shrink: 0;
  min-width: 8rem;
}

.harness-feed-item .feed-text {
  color: var(--ink);
  flex: 1;
  min-width: 0;
}

body[data-view="atomic"] #atomic-panel {
  display: block !important;
  width: 100%;
  min-height: 100vh;
  max-width: none;
  border-radius: 0;
  border: none;
  box-shadow: none;
  margin: 0;
  padding: 1.25rem;
  background: #ffffff;
}

body[data-view="atomic"] #objective-banner {
  display: grid !important;
  margin-bottom: 1rem;
  background: #ffffff;
}

body[data-view="promotion-review"] .sidebar,
body[data-view="promotion-review"] #objective-panel,
body[data-view="promotion-review"] #interrogation-panel,
body[data-view="promotion-review"] #execution-panel,
body[data-view="promotion-review"] #mermaid-panel,
body[data-view="promotion-review"] #supervisor-panel,
body[data-view="promotion-review"] #atomic-panel,
body[data-view="promotion-review"] #cli-panel,
body[data-view="promotion-review"] #step-back,
body[data-view="promotion-review"] #step-expand,
body[data-view="promotion-review"] #next-action-saved {
  display: none !important;
}

body[data-view="promotion-review"] #next-action-panel,
body[data-view="promotion-review"] #promotion-review-panel {
  display: block;
}

body[data-view="promotion-review"] .header {
  display: flex !important;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  grid-column: 1 / -1;
  padding: 0.9rem 1.25rem 0.25rem;
  border: none;
  background: #ffffff;
}

body[data-view="promotion-review"] #next-action-panel {
  grid-column: 1;
  grid-row: 2;
  border-radius: 0;
  border: none;
  box-shadow: none;
  margin: 0;
  padding: 1.25rem;
  background: #ffffff;
}

body[data-view="promotion-review"] #content-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 1rem;
  grid-column: 2;
  grid-row: 2;
  width: 100%;
  margin: 0;
  align-items: start;
}

body[data-view="promotion-review"] #promotion-review-panel {
  grid-column: 2;
  grid-row: 1;
  width: 100%;
  max-width: none;
  border-radius: 0;
  border: none;
  box-shadow: none;
  margin: 0;
  padding: 1.25rem;
  background: #fffdf8;
}

body[data-view="promotion-review"] #promotion-review-rounds-panel {
  display: block !important;
  grid-column: 1 / -1;
  grid-row: 3;
  width: 100%;
  max-width: none;
  border-radius: 0;
  border: none;
  box-shadow: none;
  margin: 0;
  padding: 1.25rem;
  background: #fffdf8;
}

body[data-view="promotion-review"] .expectation-grid,
body[data-view="promotion-review"] #proposal-actions,
body[data-view="promotion-review"] #conversation-primary-actions,
body[data-view="promotion-review"] #inline-output-panel {
  display: none !important;
}

body[data-view="promotion-review"] #next-action-title,
body[data-view="promotion-review"] #next-action-body {
  display: none !important;
}

body[data-view="promotion-review"] .conversation-transcript,
body[data-view="promotion-review"] .conversation-form {
  background: #ffffff;
  color: var(--ink);
}

body[data-view="objective-create"] .sidebar,
body[data-view="objective-create"] #next-action-panel,
body[data-view="objective-create"] #objective-panel,
body[data-view="objective-create"] #interrogation-panel,
body[data-view="objective-create"] #mermaid-panel,
body[data-view="objective-create"] #execution-panel,
body[data-view="objective-create"] #supervisor-panel,
body[data-view="objective-create"] #atomic-panel,
body[data-view="objective-create"] #promotion-review-panel,
body[data-view="objective-create"] #promotion-review-rounds-panel,
body[data-view="objective-create"] #cli-panel {
  display: none !important;
}

body[data-view="objective-create"] .header {
  display: flex !important;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem 1.25rem 0.25rem;
  background: #ffffff;
  border: none;
}

body[data-view="objective-create"] #content-grid {
  display: block;
  width: 100%;
  margin: 0;
}

body[data-view="objective-create"] #new-objective-panel {
  display: block !important;
  width: min(860px, 100%);
  margin: 0 auto;
  border: none;
  box-shadow: none;
  border-radius: 0;
  padding: 1.5rem 1.25rem 2rem;
  background: #ffffff;
}

body[data-view="objective-create"] .header-actions {
  display: none;
}

body[data-view="token-performance"] .sidebar,
body[data-view="token-performance"] #next-action-panel,
body[data-view="token-performance"] #objective-panel,
body[data-view="token-performance"] #interrogation-panel,
body[data-view="token-performance"] #mermaid-panel,
body[data-view="token-performance"] #execution-panel,
body[data-view="token-performance"] #supervisor-panel,
body[data-view="token-performance"] #atomic-panel,
body[data-view="token-performance"] #promotion-review-panel,
body[data-view="token-performance"] #promotion-review-rounds-panel,
body[data-view="token-performance"] #cli-panel,
body[data-view="token-performance"] #new-objective-panel {
  display: none !important;
}

body[data-view="token-performance"] .header {
  display: flex !important;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem 1.25rem 0.25rem;
  background: #ffffff;
  border: none;
}

body[data-view="token-performance"] #content-grid {
  display: block;
  width: 100%;
  margin: 0;
}

body[data-view="token-performance"] #token-performance-panel {
  display: block !important;
  width: min(1280px, 100%);
  margin: 0 auto;
  border: none;
  box-shadow: none;
  border-radius: 0;
  padding: 1.25rem;
  background: #ffffff;
}

body[data-view="settings"] .sidebar,
body[data-view="settings"] #next-action-panel,
body[data-view="settings"] #objective-panel,
body[data-view="settings"] #interrogation-panel,
body[data-view="settings"] #mermaid-panel,
body[data-view="settings"] #execution-panel,
body[data-view="settings"] #supervisor-panel,
body[data-view="settings"] #atomic-panel,
body[data-view="settings"] #promotion-review-panel,
body[data-view="settings"] #promotion-review-rounds-panel,
body[data-view="settings"] #cli-panel,
body[data-view="settings"] #new-objective-panel,
body[data-view="settings"] #token-performance-panel {
  display: none !important;
}

body[data-view="settings"] .header {
  display: flex !important;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem 1.25rem 0.25rem;
  background: #ffffff;
  border: none;
}

body[data-view="settings"] #content-grid {
  display: block;
  width: 100%;
  margin: 0;
}

body[data-view="settings"] #settings-panel {
  display: block !important;
  width: min(920px, 100%);
  margin: 0 auto;
  border: none;
  box-shadow: none;
  border-radius: 0;
  padding: 1.25rem;
  background: #ffffff;
}

.settings-shell {
  display: grid;
  gap: 1.25rem;
}

.settings-hero {
  padding: 1rem 1.1rem;
  border: 1px solid var(--line);
  border-radius: 1rem;
  background:
    radial-gradient(circle at top right, rgba(162, 76, 43, 0.12), transparent 30%),
    linear-gradient(135deg, #fff9f0 0%, #fffef9 100%);
}

.settings-hero h3 {
  margin: 0 0 0.4rem;
}

.settings-card {
  padding: 1rem 1.1rem 1.15rem;
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: #fffdf8;
}

.settings-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.9rem 1rem;
}

.settings-grid label {
  display: grid;
  gap: 0.35rem;
  font-size: 0.9rem;
  color: var(--ink);
}

.settings-grid label.full {
  grid-column: 1 / -1;
}

.settings-helper {
  color: var(--muted);
  font-size: 0.86rem;
}

.settings-actions {
  display: flex;
  align-items: center;
  gap: 0.85rem;
  margin-top: 1rem;
}

.settings-save-button {
  position: relative;
  min-width: 10.5rem;
  transition: transform 160ms ease, box-shadow 200ms ease, background-color 200ms ease;
}

.settings-save-button:not(:disabled):hover {
  transform: translateY(-1px);
}

.settings-save-button.is-saving {
  background: linear-gradient(90deg, #a24c2b, #c86a3d, #a24c2b);
  background-size: 200% 100%;
  animation: settings-save-sheen 1s linear infinite;
}

.settings-save-button.is-saved {
  background: linear-gradient(135deg, #2f6f4f, #4f8f6f);
  box-shadow: 0 0 0 0 rgba(47, 111, 79, 0.4);
  animation: settings-save-pop 550ms ease;
}

.settings-save-button:disabled:not(.is-saved):not(.is-saving) {
  cursor: default;
}

.settings-save-status {
  min-height: 1.25rem;
  color: var(--muted);
  font-size: 0.86rem;
  opacity: 0;
  transform: translateY(4px);
  transition: opacity 180ms ease, transform 180ms ease;
}

.settings-save-status.visible {
  opacity: 1;
  transform: translateY(0);
}

.settings-save-status.success {
  color: var(--success);
}

.modal-overlay {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.5rem;
  background: rgba(31, 41, 51, 0.42);
  backdrop-filter: blur(2px);
  z-index: 40;
}

.modal-overlay[hidden] {
  display: none !important;
}

.modal-card {
  width: min(560px, 100%);
  border-radius: 1.1rem;
  border: 1px solid rgba(223, 211, 184, 0.9);
  background:
    radial-gradient(circle at top right, rgba(162, 76, 43, 0.08), transparent 30%),
    #fffdf8;
  box-shadow: 0 24px 60px rgba(31, 41, 51, 0.24);
  padding: 1.15rem 1.15rem 1rem;
}

.modal-card.working {
  border-color: #d8b08e;
  background:
    radial-gradient(circle at top right, rgba(217, 178, 106, 0.18), transparent 34%),
    linear-gradient(135deg, #fff6eb 0%, #fffdfa 100%);
}

.modal-card.success {
  border-color: #9fc9ab;
  background:
    radial-gradient(circle at top right, rgba(47, 111, 79, 0.14), transparent 34%),
    linear-gradient(135deg, #eff9f1 0%, #fbfffc 100%);
}

.modal-card.error {
  border-color: #d8b08e;
  background:
    radial-gradient(circle at top right, rgba(162, 76, 43, 0.12), transparent 34%),
    linear-gradient(135deg, #fff4ea 0%, #fffdfa 100%);
}

.modal-title {
  margin: 0;
  font-size: 1.15rem;
}

.modal-body {
  margin-top: 0.7rem;
  color: var(--ink);
  line-height: 1.5;
  white-space: pre-line;
}

.modal-status-row {
  display: none;
  align-items: center;
  gap: 0.7rem;
  margin-top: 0.9rem;
}

.modal-card.working .modal-status-row {
  display: flex;
}

.modal-spinner {
  width: 1rem;
  height: 1rem;
  border-radius: 999px;
  border: 2px solid rgba(191, 98, 43, 0.2);
  border-top-color: #bf622b;
  animation: modal-spin 0.85s linear infinite;
}

.modal-status-text {
  color: #8a461e;
  font-size: 0.92rem;
  font-weight: 600;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.7rem;
  margin-top: 1rem;
}

@keyframes settings-save-pop {
  0% { transform: scale(0.96); box-shadow: 0 0 0 0 rgba(47, 111, 79, 0.4); }
  45% { transform: scale(1.03); box-shadow: 0 0 0 10px rgba(47, 111, 79, 0); }
  100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(47, 111, 79, 0); }
}

@keyframes modal-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

@keyframes settings-save-sheen {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

body[data-view="atomic"] .atomic-objective-picker,
body[data-view="control-flow"] .atomic-objective-picker,
body[data-view="promotion-review"] .atomic-objective-picker {
  display: block;
}

.atomic-objective-picker {
  display: none;
}

.atomic-objective-picker select {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 0.8rem;
  padding: 0.55rem 0.7rem;
  font: inherit;
  background: #fff;
}

body[data-view="atomic"] .expectation-grid,
body[data-view="atomic"] #next-action-title,
body[data-view="atomic"] #next-action-body {
  display: none !important;
}

body[data-view="atomic"] .conversation-transcript,
body[data-view="atomic"] .conversation-form {
  background: #ffffff;
  color: var(--ink);
}

.atomic-generation-meta {
  display: flex;
  gap: 0.9rem;
  flex-wrap: wrap;
  color: var(--muted);
  font-size: 0.9rem;
  margin-bottom: 0.85rem;
}

.objective-create-shell {
  display: grid;
  gap: 1rem;
}

.objective-create-hero {
  display: grid;
  gap: 0.45rem;
  padding: 1rem 1.1rem;
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: linear-gradient(135deg, #fffdf8 0%, #f8f1e4 100%);
}

.objective-create-hero h3 {
  margin: 0;
  font-size: 1.35rem;
}

.objective-create-hero p {
  margin: 0;
  color: var(--muted);
}

.objective-create-form {
  display: grid;
  gap: 1rem;
}

.objective-create-form .field-grid {
  display: grid;
  gap: 0.9rem;
}

.objective-create-form label {
  display: grid;
  gap: 0.4rem;
  font-size: 0.9rem;
  color: var(--muted);
  font-weight: 600;
}

.objective-create-form input,
.objective-create-form textarea,
.objective-create-form select {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 0.85rem;
  padding: 0.7rem 0.8rem;
  font: inherit;
  background: #fff;
  color: var(--ink);
}

.objective-create-form textarea {
  min-height: 10rem;
  resize: vertical;
}

.objective-create-actions {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  flex-wrap: wrap;
}

.objective-create-cancel {
  color: var(--muted);
  text-decoration: none;
  font-weight: 600;
}

.token-performance-shell {
  display: grid;
  gap: 1rem;
}

.token-performance-hero {
  display: grid;
  gap: 0.35rem;
  padding: 1rem 1.1rem;
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: linear-gradient(135deg, #fffdf8 0%, #f3ede2 100%);
}

.token-performance-hero h3 {
  margin: 0;
  font-size: 1.35rem;
}

.token-performance-hero p {
  margin: 0;
  color: var(--muted);
}

.token-performance-grid {
  display: grid;
  gap: 1rem;
}

.token-performance-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 0.8rem;
}

.token-performance-card,
.token-performance-table,
.token-performance-note {
  border: 1px solid var(--line);
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.88);
  padding: 0.9rem 1rem;
}

.token-performance-card .label {
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
}

.token-performance-card .value {
  margin-top: 0.35rem;
  font-size: 1.2rem;
  font-weight: 700;
}

.token-performance-table h4,
.token-performance-note h4 {
  margin: 0 0 0.75rem 0;
}

.token-performance-table table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
}

.token-performance-table th,
.token-performance-table td {
  text-align: left;
  padding: 0.55rem 0.45rem;
  border-top: 1px solid var(--line);
  vertical-align: top;
}

.token-performance-table thead th {
  border-top: none;
  color: var(--muted);
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.atomic-generation-meta .pill {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0.24rem 0.6rem;
  background: #fffdf8;
}

.atomic-generation-meta .pill.status-running {
  border-color: #d9b26a;
  background: #fff5df;
  color: #8b5a00;
}

.atomic-generation-meta .pill.status-complete {
  border-color: #8fc8a5;
  background: #edf9f0;
  color: #1f6b35;
}

.atomic-generation-meta .pill.status-failed {
  border-color: #d49b9b;
  background: #fff1f1;
  color: #9f2f2f;
}

.atomic-generation-meta .pill.status-idle {
  border-color: #cfd6df;
  background: #f6f8fb;
  color: #536273;
}

.atomic-generation-meta .pill.live::before {
  content: "";
  width: 0.48rem;
  height: 0.48rem;
  border-radius: 999px;
  background: var(--accent);
  animation: atomicPulse 1.2s ease-in-out infinite;
}

@keyframes atomicPulse {
  0%, 100% { opacity: 0.35; transform: scale(0.9); }
  50% { opacity: 1; transform: scale(1.05); }
}

.atomic-status-tabs {
  display: flex;
  gap: 0;
  margin-bottom: 0.5rem;
  border-bottom: 2px solid var(--line);
}

.atomic-status-tabs button {
  padding: 0.4rem 0.85rem;
  border: none;
  background: none;
  cursor: pointer;
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--muted);
  border-bottom: 2px solid transparent;
  margin-bottom: -2px;
  transition: color 0.15s, border-color 0.15s;
}

.atomic-status-tabs button:hover {
  color: var(--ink);
}

.atomic-status-tabs button.active {
  color: var(--ink);
  border-bottom-color: var(--accent);
}

.atomic-status-tabs button .tab-count {
  font-weight: 400;
  font-size: 0.78rem;
  color: var(--muted);
  margin-left: 0.25rem;
}

.atomic-progress-bar {
  display: flex;
  height: 0.5rem;
  border-radius: 0.25rem;
  overflow: hidden;
  background: #e8e2d4;
  margin-bottom: 0.5rem;
}

.atomic-progress-bar .segment {
  transition: width 0.3s ease;
}

.atomic-progress-bar .segment.completed { background: #2f6f4f; }
.atomic-progress-bar .segment.active { background: #d9b26a; }
.atomic-progress-bar .segment.failed { background: #c0504d; }
.atomic-progress-bar .segment.pending { background: #dfd3b8; }

.atomic-progress-summary {
  font-size: 0.82rem;
  color: var(--muted);
  margin-bottom: 0.75rem;
}

.view-nav {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin: 0.75rem 0 0;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
  margin-top: 0.75rem;
}

.header-button {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.45rem 0.85rem;
  border-radius: 999px;
  border: 1px solid #d8b08e;
  background: #fff1e5;
  color: #8a461e;
  font-size: 0.85rem;
  font-weight: 700;
  cursor: pointer;
}

.header-button:hover {
  background: #ffe8d7;
}

.view-nav-link {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.45rem 0.75rem;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.7);
  color: var(--ink);
  text-decoration: none;
  font-size: 0.85rem;
}

.view-nav-link.active {
  background: #fff1e5;
  border-color: #d8b08e;
  color: #8a461e;
}

.promotion-grid {
  display: grid;
  gap: 1rem;
}

.promotion-summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 0.8rem;
}

.promotion-summary-card,
.promotion-packet,
.promotion-failed-task {
  border: 1px solid var(--line);
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.84);
  padding: 0.85rem 1rem;
}

.promotion-summary-card .label,
.promotion-packet .label,
.promotion-failed-task .label {
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
}

.promotion-summary-card .value {
  margin-top: 0.35rem;
  font-size: 1.2rem;
  font-weight: 700;
}

.promotion-summary-card.repo-promotion-success {
  border-color: #9fc9ab;
  background:
    radial-gradient(circle at top right, rgba(47, 111, 79, 0.16), transparent 32%),
    linear-gradient(135deg, #eff9f1 0%, #fbfffc 100%);
}

.repo-promotion-success-title {
  margin: 0 0 0.45rem;
  color: var(--success);
  font-size: 1.1rem;
}

.repo-promotion-success-copy {
  color: #22563d;
  font-size: 0.95rem;
  line-height: 1.45;
}

.repo-promotion-success-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
  margin-top: 0.8rem;
}

.repo-promotion-success-link {
  color: var(--success);
  font-weight: 600;
  text-decoration: none;
}

.repo-promotion-success-link:hover {
  text-decoration: underline;
}

.promotion-primary-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  padding: 0.72rem 1.1rem;
  border: 1px solid #8a461e;
  border-radius: 999px;
  background:
    linear-gradient(135deg, #a24c2b 0%, #c56a3a 100%);
  color: #fffdf9;
  font-size: 0.92rem;
  font-weight: 700;
  letter-spacing: 0.01em;
  box-shadow: 0 10px 24px rgba(162, 76, 43, 0.18);
  transition: transform 160ms ease, box-shadow 180ms ease, filter 180ms ease;
}

.promotion-primary-button:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 14px 28px rgba(162, 76, 43, 0.24);
  filter: saturate(1.04);
}

.promotion-primary-button:active:not(:disabled) {
  transform: translateY(0);
}

.promotion-primary-button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
  box-shadow: none;
}

.promotion-section-title {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 0.75rem;
  margin: 0.2rem 0 0.75rem;
}

.promotion-section-title h4 {
  margin: 0;
}

.promotion-packet-list,
.promotion-failed-list {
  display: grid;
  gap: 0.85rem;
}

.promotion-round-list {
  display: grid;
  gap: 1rem;
}

.promotion-round {
  display: grid;
  gap: 0.85rem;
  padding: 0.9rem 1rem 1rem;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.74);
}

.promotion-round-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.65rem;
}

.promotion-latest-round {
  display: grid;
  gap: 0.8rem;
  padding: 1rem;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: linear-gradient(180deg, rgba(255,255,255,0.92), rgba(251,246,238,0.92));
  margin-bottom: 0.95rem;
}

.promotion-latest-round h4 {
  margin: 0;
}

.promotion-state-banner {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.95rem 1rem;
  border-radius: 16px;
  border: 2px solid var(--accent);
  background: linear-gradient(135deg, rgba(201, 117, 53, 0.14), rgba(255, 246, 234, 0.92));
}

.promotion-state-banner.status-complete {
  border-color: var(--ok);
  background: linear-gradient(135deg, rgba(46, 127, 84, 0.14), rgba(245, 253, 248, 0.94));
}

.promotion-state-banner.status-failed {
  border-color: var(--danger);
  background: linear-gradient(135deg, rgba(184, 84, 80, 0.14), rgba(255, 245, 244, 0.94));
}

.promotion-state-banner-icon {
  width: 2.4rem;
  height: 2.4rem;
  border-radius: 999px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  color: white;
  background: var(--accent);
  flex: 0 0 auto;
}

.promotion-state-banner.status-complete .promotion-state-banner-icon {
  background: var(--ok);
}

.promotion-state-banner.status-failed .promotion-state-banner-icon {
  background: var(--danger);
}

.promotion-state-banner-copy {
  display: grid;
  gap: 0.18rem;
}

.promotion-state-banner-copy strong {
  font-size: 1rem;
}

.promotion-reviewer-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.promotion-report-card {
  display: grid;
  gap: 0.75rem;
  padding: 0.95rem 1rem;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.88);
  margin-bottom: 0.95rem;
}

.promotion-report-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 0.75rem;
}

.promotion-report-card-cell {
  display: grid;
  gap: 0.35rem;
  justify-items: center;
  align-content: start;
  min-height: 118px;
  padding: 0.8rem 0.6rem;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: #fbf6ee;
  text-align: center;
  cursor: pointer;
}

.promotion-report-card-cell .emoji {
  font-size: 2rem;
  line-height: 1;
}

.promotion-report-card-cell .name {
  font-size: 0.84rem;
  font-weight: 700;
}

.promotion-report-card-cell .sub {
  font-size: 0.76rem;
  color: var(--muted);
}

.promotion-report-card-cell.active {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(201, 117, 53, 0.12);
}

.promotion-report-card-detail {
  border: 1px solid var(--line);
  border-radius: 16px;
  background: rgba(255,255,255,0.94);
  padding: 0.9rem 1rem;
}

.promotion-round-stat {
  border: 1px solid var(--line);
  border-radius: 14px;
  background: rgba(251, 246, 238, 0.92);
  padding: 0.65rem 0.75rem;
}

.promotion-round-stat .label {
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
}

.promotion-round-stat .value {
  margin-top: 0.22rem;
  font-size: 1rem;
  font-weight: 700;
  color: var(--ink);
}

.promotion-round-stat .value.status-complete {
  color: var(--ok);
}

.promotion-round-stat .value.status-running {
  color: var(--accent);
}

.promotion-round-stat .value.status-failed {
  color: var(--danger);
}

.promotion-packet-meta,
.promotion-failed-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
  margin: 0.45rem 0 0.6rem;
}

.promotion-packet-summary,
.promotion-failed-body {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.92rem;
  line-height: 1.45;
  background: #fbf6ee;
  border-radius: 12px;
  padding: 0.75rem;
}

.promotion-json-block {
  white-space: pre-wrap;
  word-break: break-word;
  font-family: "IBM Plex Mono", monospace;
  font-size: 0.82rem;
  background: #fbf6ee;
  border-radius: 12px;
  padding: 0.75rem;
}

.promotion-packet-summary {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.95rem;
  line-height: 1.5;
  color: var(--ink);
}

.promotion-packet-title {
  margin-top: 0.2rem;
  font-size: 1.05rem;
  font-weight: 700;
}

.promotion-verdict-pill {
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-width: 2px;
  font-size: 0.84rem;
  padding: 0.32rem 0.62rem;
}

.promotion-opinion-pill {
  font-weight: 700;
}

.promotion-packet-evidence {
  margin: 0.55rem 0 0;
  padding-left: 1rem;
  color: var(--muted);
}

.promotion-packet-evidence li {
  margin: 0.15rem 0;
  font-size: 0.86rem;
}

.promotion-requirements {
  display: grid;
  gap: 0.65rem;
  margin-top: 0.75rem;
}

.promotion-requirement-block {
  border: 1px solid var(--line);
  border-left: 4px solid var(--accent);
  border-radius: 12px;
  background: #fffdf8;
  padding: 0.7rem 0.8rem;
}

.promotion-requirement-body {
  margin-top: 0.28rem;
  font-size: 0.9rem;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

.promotion-packet-issues {
  margin: 0.6rem 0 0;
  padding-left: 1rem;
}

.promotion-packet-issues li {
  margin: 0.2rem 0;
}

.atomic-list {
  display: grid;
  gap: 0.35rem;
}

.atomic-card {
  border: 1px solid var(--line);
  border-radius: 0.6rem;
  padding: 0;
  background: #fffdf8;
  display: flex;
  overflow: hidden;
  cursor: pointer;
  transition: box-shadow 0.15s ease;
}

.atomic-card:hover {
  box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}

.atomic-card.active {
  border-color: var(--accent);
  box-shadow: 0 4px 16px rgba(162, 76, 43, 0.1);
}

.atomic-card .status-bar {
  width: 4px;
  flex-shrink: 0;
}

.atomic-card .status-bar.pending { background: #b8b0a0; }
.atomic-card .status-bar.active, .atomic-card .status-bar.working { background: #4a90d9; }
.atomic-card .status-bar.validating { background: #2f6f4f; }
.atomic-card .status-bar.completed { background: #2f6f4f; }
.atomic-card .status-bar.failed { background: #c0504d; }

.atomic-card .card-content {
  flex: 1;
  padding: 0.55rem 0.75rem;
  min-width: 0;
}

.atomic-card .card-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.atomic-card .title {
  font-weight: 600;
  font-size: 0.92rem;
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.atomic-card .status-pill {
  font-size: 0.72rem;
  padding: 0.1rem 0.45rem;
  border-radius: 0.3rem;
  font-weight: 600;
  flex-shrink: 0;
}

.atomic-card .status-pill.pending { background: #f0ebe0; color: #7a7060; }
.atomic-card .status-pill.active, .atomic-card .status-pill.working { background: #e3effa; color: #1a5294; }
.atomic-card .status-pill.validating { background: #e3f5e8; color: #1a6b35; }
.atomic-card .status-pill.completed { background: #edf9f0; color: #1f6b35; }
.atomic-card .status-pill.failed { background: #fff1f1; color: #9f2f2f; }

.atomic-card .validation-summary {
  font-size: 0.72rem;
  color: var(--muted);
  margin-top: 0.15rem;
  display: none;
}
.atomic-card.expanded .validation-summary,
.atomic-card .validation-summary.inline { display: block; }
.validation-summary .pass { color: #1f6b35; }
.validation-summary .fail { color: #9f2f2f; }

.atomic-card .attempt-count {
  font-size: 0.72rem;
  color: var(--muted);
  flex-shrink: 0;
}

.atomic-card .runtime {
  font-size: 0.72rem;
  color: var(--muted);
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

.atomic-card .retry-btn {
  font-size: 0.75rem;
  padding: 0.2rem 0.6rem;
  margin-top: 0.4rem;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface);
  color: var(--fg);
  cursor: pointer;
}
.atomic-card .retry-btn:hover { background: var(--hover); }

.retry-all-btn {
  font-size: 0.75rem;
  padding: 0.2rem 0.6rem;
  margin-left: auto;
  border: 1px solid #c0504d;
  border-radius: 4px;
  background: transparent;
  color: #c0504d;
  cursor: pointer;
}
.retry-all-btn:hover { background: #fff1f1; }

.atomic-card .meta {
  color: var(--muted);
  font-size: 0.82rem;
  margin-top: 0.15rem;
  display: none;
}

.atomic-card.expanded .meta {
  display: block;
}

.atomic-card .body {
  white-space: pre-wrap;
  font-size: 0.85rem;
  margin-top: 0.35rem;
  display: none;
  color: var(--ink);
  line-height: 1.45;
}

.atomic-card.expanded .body {
  display: block;
}

.app-shell.sidebar-collapsed {
  grid-template-columns: 56px 1fr;
}

.sidebar {
  padding: 1rem;
  border-right: 1px solid var(--line);
  background: rgba(255, 250, 240, 0.82);
  backdrop-filter: blur(8px);
  overflow: hidden;
}

.sidebar-toggle {
  width: 100%;
  border: 1px solid var(--line);
  background: #fff;
  border-radius: 999px;
  padding: 0.45rem 0.7rem;
  cursor: pointer;
  font: inherit;
  margin-bottom: 0.85rem;
}

.sidebar-body {
  display: block;
}

.app-shell.sidebar-collapsed .sidebar {
  padding-inline: 0.45rem;
}

.app-shell.sidebar-collapsed .sidebar-body {
  display: none;
}

.app-shell.sidebar-collapsed .sidebar-toggle {
  padding-inline: 0;
}

.sidebar h1 {
  margin: 0 0 0.35rem;
  font-size: 1.2rem;
}

.sidebar .subtle {
  margin: 0 0 1rem;
  color: var(--muted);
  font-size: 0.92rem;
}

.selector {
  width: 100%;
  padding: 0.7rem 0.8rem;
  border-radius: 0.8rem;
  border: 1px solid var(--line);
  background: #fff;
}

.list {
  margin-top: 1rem;
  display: grid;
  gap: 0.65rem;
}

.list button {
  text-align: left;
  width: 100%;
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 1rem;
  padding: 0.8rem;
  cursor: pointer;
}

.list button.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.list .title {
  display: block;
  font-weight: 700;
}

.list .meta {
  display: block;
  margin-top: 0.2rem;
  color: var(--muted);
  font-size: 0.88rem;
}

.section-title {
  font-size: 0.95rem;
  font-weight: 700;
  margin: 0.9rem 0 0.4rem;
}

.content {
  padding: 1rem;
  display: grid;
  gap: 1rem;
}

.content.mode-mermaid-review {
  grid-template-columns: minmax(420px, 1fr) minmax(420px, 1fr);
  align-items: start;
}

.content.mode-mermaid-review .header {
  grid-column: 1 / -1;
}

.content.mode-mermaid-review #next-action-panel {
  grid-column: 1;
  grid-row: 2;
}

.content.mode-mermaid-review #content-grid {
  grid-column: 2;
  grid-row: 2;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: end;
  gap: 1rem;
}

.header h2 {
  margin: 0;
  font-size: 1.4rem;
}

.header p {
  margin: 0.35rem 0 0;
  color: var(--muted);
}

.status-chip {
  padding: 0.4rem 0.7rem;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: var(--panel);
  font-size: 0.88rem;
  white-space: nowrap;
}

.grid {
  display: grid;
  grid-template-columns: minmax(360px, 1.15fr) minmax(360px, 1fr) minmax(320px, 0.8fr);
  gap: 1rem;
  align-items: start;
}

.grid.focused {
  grid-template-columns: minmax(0, 1fr);
}

.grid.hidden {
  display: none;
}

.panel {
  background: rgba(255, 250, 240, 0.88);
  border: 1px solid var(--line);
  border-radius: 1.2rem;
  padding: 1rem;
  box-shadow: 0 12px 30px rgba(74, 53, 28, 0.08);
}

.panel h3 {
  margin: 0 0 0.25rem;
}

.panel[hidden] {
  display: none !important;
}

.step-actions {
  display: flex;
  gap: 0.65rem;
  margin-top: 0.75rem;
}

.step-actions button {
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fff;
  color: var(--ink);
  padding: 0.65rem 0.9rem;
  cursor: pointer;
  font: inherit;
}

.expectation-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.75rem;
  margin: 0.85rem 0 0.25rem;
}

.expectation-card {
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: #fff;
  padding: 0.8rem 0.9rem;
}

.expectation-card .label {
  color: var(--muted);
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 0.3rem;
}

.expectation-card .value {
  line-height: 1.45;
}

.saved-answer {
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: #fff;
  padding: 0.8rem 0.9rem;
  margin-bottom: 0.8rem;
  cursor: pointer;
}

.saved-answer .meta {
  color: var(--muted);
  font-size: 0.82rem;
  margin-bottom: 0.35rem;
}

.saved-answer:hover {
  border-color: var(--accent);
  box-shadow: 0 8px 24px rgba(162, 76, 43, 0.08);
}

.conversation-transcript {
  display: grid;
  gap: 0.75rem;
  margin-top: 0.85rem;
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: #171c24;
  color: #e7edf4;
  padding: 0.8rem;
  min-height: 300px;
  max-height: 60vh;
  overflow: auto;
  font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
  white-space: pre-wrap;
}

.transcript-bubble {
  border-left: 3px solid #45617f;
  padding: 0.3rem 0 0.3rem 0.9rem;
}

.transcript-bubble.operator {
  border-left-color: #a24c2b;
}

.transcript-bubble.system {
  border-left-color: #64748b;
}

.transcript-bubble .meta {
  color: #8ba3bc;
  font-size: 0.82rem;
  margin-bottom: 0.35rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.conversation-form {
  display: grid;
  gap: 0.75rem;
  margin-top: 0.85rem;
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.72);
  padding: 0.8rem;
}

.conversation-primary-actions {
  margin-top: 0.75rem;
}

.diagram-comment-anchor {
  display: none;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.7rem 0.85rem;
  border: 1px solid var(--line);
  border-radius: 0.85rem;
  background: #fff9ef;
  color: var(--ink);
}

.diagram-comment-anchor.visible {
  display: flex;
}

.diagram-comment-anchor .meta {
  color: var(--muted);
  font-size: 0.82rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 0.2rem;
}

.diagram-comment-anchor .value {
  font-weight: 600;
}

.conversation-textarea {
  min-height: 180px;
}

.conversation-textarea.stage-compact {
  min-height: 120px;
}

.conversation-label {
  color: var(--muted);
  font-size: 0.82rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.step-prompt {
  border: 2px solid var(--accent);
  border-radius: 1rem;
  background: linear-gradient(135deg, #fff8ef 0%, #f8e0d0 100%);
  padding: 1rem 1.05rem;
  margin-bottom: 0.85rem;
  box-shadow: 0 10px 28px rgba(162, 76, 43, 0.12);
}

.step-prompt .question {
  font-weight: 700;
  font-size: 1.08rem;
  margin-bottom: 0.35rem;
}

.step-prompt .helper {
  color: var(--muted);
  font-size: 0.95rem;
}

.step-prompt.pulse {
  animation: stepPulse 900ms ease;
}

.step-target {
  scroll-margin-top: 24px;
}

.step-target.pulse {
  animation: targetLift 700ms ease;
}

@keyframes stepPulse {
  0% { transform: translateY(8px); opacity: 0.35; }
  100% { transform: translateY(0); opacity: 1; }
}

@keyframes targetLift {
  0% { transform: translateY(14px); box-shadow: 0 0 0 rgba(162, 76, 43, 0); }
  45% { transform: translateY(0); box-shadow: 0 12px 30px rgba(162, 76, 43, 0.16); }
  100% { transform: translateY(0); box-shadow: none; }
}

.panel .hint {
  margin: 0 0 1rem;
  color: var(--muted);
  font-size: 0.92rem;
}

.diagram-shell {
  min-height: 420px;
  overflow: auto;
  border-radius: 1rem;
  border: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.7);
  padding: 0.75rem;
  position: relative;
}

.diagram-shell svg {
  max-width: 100%;
  height: auto;
}

.diagram-shell.panning {
  cursor: grabbing;
}

.diagram-shell svg [data-clickable-mermaid="1"] {
  cursor: pointer;
}

.diagram-shell svg .diagram-selected rect,
.diagram-shell svg .diagram-selected polygon,
.diagram-shell svg .diagram-selected path:not(.flowchart-link):not(.edge-thickness-normal):not(.edge-thickness-thick) {
  stroke: #a24c2b !important;
  stroke-width: 3px !important;
}

.diagram-shell.updating {
  opacity: 0.5;
  transition: opacity 140ms ease;
}

.diagram-shell.updating::after {
  content: "Updating diagram...";
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ink);
  font-weight: 700;
  background: rgba(255, 255, 255, 0.55);
  backdrop-filter: blur(1px);
  pointer-events: none;
}

.diagram-shell.locked {
  position: relative;
}

.diagram-shell.locked::before {
  content: "Accepted control flow";
  position: absolute;
  top: 0.85rem;
  right: 0.85rem;
  z-index: 2;
  padding: 0.35rem 0.6rem;
  border-radius: 999px;
  background: rgba(17, 24, 39, 0.78);
  color: #fff;
  font-size: 0.82rem;
  font-weight: 700;
}

.diagram-shell.locked::after {
  content: "🔒";
  position: absolute;
  inset: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: clamp(7rem, 16vw, 12rem);
  font-weight: 700;
  color: rgba(17, 24, 39, 0.5);
  pointer-events: none;
}

.diagram-shell.locked svg {
  opacity: 0.88;
}

.output-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}

.output-tabs button {
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fff;
  padding: 0.4rem 0.7rem;
  cursor: pointer;
  font-size: 0.85rem;
}

.output-tabs button.active {
  background: var(--accent-soft);
  border-color: var(--accent);
}

.output-body {
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: #1d232c;
  color: #e8edf2;
  padding: 0.9rem;
  min-height: 420px;
  max-height: 70vh;
  overflow: auto;
  font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
  white-space: pre-wrap;
  word-break: break-word;
}

.inline-output-panel {
  margin-top: 0.85rem;
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.76);
  padding: 0.8rem;
}

.inline-output-panel[hidden] {
  display: none !important;
}

.summary-list {
  margin: 0.5rem 0 0;
  padding-left: 1.1rem;
}

.comment-list {
  display: grid;
  gap: 0.7rem;
  max-height: 380px;
  overflow: auto;
  margin-bottom: 1rem;
}

.comment {
  border: 1px solid var(--line);
  border-radius: 1rem;
  padding: 0.85rem;
  background: #fff;
}

.comment .meta {
  color: var(--muted);
  font-size: 0.82rem;
  margin-bottom: 0.35rem;
}

textarea, input {
  width: 100%;
  border-radius: 0.9rem;
  border: 1px solid var(--line);
  padding: 0.75rem 0.85rem;
  font: inherit;
  background: #fff;
}

textarea {
  min-height: 140px;
  resize: vertical;
}

.form-row {
  display: grid;
  gap: 0.65rem;
}

.actions {
  display: flex;
  gap: 0.65rem;
  margin-top: 0.75rem;
}

.actions button {
  border: none;
  border-radius: 999px;
  background: var(--accent);
  color: #fff9f0;
  padding: 0.7rem 1rem;
  cursor: pointer;
  font: inherit;
}

.empty {
  color: var(--muted);
  font-style: italic;
}

.error {
  border: 1px solid #d27057;
  background: #fbe1d8;
  color: #7f2d1b;
  border-radius: 1rem;
  padding: 0.8rem 1rem;
}

.execution-summary {
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: #fff;
  padding: 0.95rem 1rem;
  display: grid;
  gap: 0.65rem;
}

.execution-summary .label {
  color: var(--muted);
  font-size: 0.82rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.execution-summary .value {
  font-weight: 700;
}

.execution-summary .body {
  line-height: 1.45;
}

@media (max-width: 1180px) {
  .app-shell { grid-template-columns: 1fr; }
  .sidebar { border-right: none; border-bottom: 1px solid var(--line); }
  .grid { grid-template-columns: 1fr; }
  .expectation-grid { grid-template-columns: 1fr; }
  .content.mode-mermaid-review { grid-template-columns: 1fr; }
  .content.mode-mermaid-review #next-action-panel,
  .content.mode-mermaid-review #content-grid { grid-column: 1; grid-row: auto; }
  body[data-view="control-flow"] .content { grid-template-columns: 1fr; }
}

@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #111317;
    --panel: #171b20;
    --ink: #eef2f6;
    --muted: #aab5c1;
    --line: #313943;
    --accent: #d46d45;
    --accent-soft: #3a241b;
    --success: #4f9870;
  }

  body {
    background:
      radial-gradient(circle at top left, #1a1d22 0, #111317 55%),
      linear-gradient(135deg, #171b20 0, #111317 100%);
  }

  .sidebar,
  .panel,
  .conversation-form,
  .inline-output-panel,
  .execution-summary,
  .saved-answer,
  textarea,
  input,
  .list button,
  .selector,
  .sidebar-toggle,
  .step-actions button,
  .output-tabs button {
    background: var(--panel);
    color: var(--ink);
  }

  .diagram-shell {
    background: #fff;
  }

  .diagram-shell svg {
    background: #fff;
    border-radius: 0.8rem;
  }
}
"""


_APP_JS = r"""
let mermaid = null;
let mermaidLoadPromise = null;

async function ensureMermaid() {
  if (mermaid) return mermaid;
  if (!mermaidLoadPromise) {
    mermaidLoadPromise = import('https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs')
      .then((module) => {
        const instance = module.default;
        instance.initialize({
          startOnLoad: false,
          theme: 'default',
          securityLevel: 'loose',
        });
        mermaid = instance;
        return instance;
      });
  }
  return mermaidLoadPromise;
}

const state = {
  projects: [],
  projectId: localStorage.getItem('accruvia.ui.projectId'),
  objectiveId: localStorage.getItem('accruvia.ui.objectiveId'),
  taskId: null,
  runId: null,
  workspace: null,
  runOutput: null,
  activeSectionPath: null,
  sidebarCollapsed: localStorage.getItem('accruvia.ui.sidebarCollapsed') === '1',
  expandAll: false,
  lastSavedStep: null,
  manualFocusMode: null,
  suppressFocusAnimation: false,
  showInlineReview: false,
  conversationPending: false,
  diagramUpdating: false,
  diagramPan: { scale: 1, x: 0, y: 0, isPointerDown: false, isDragging: false, startX: 0, startY: 0, dragOriginX: 0, dragOriginY: 0 },
  localNotices: [],
  view: document.body.dataset.view || 'default',
  atomicTab: 'all',
  manualViewOverride: sessionStorage.getItem('accruvia.ui.manualViewOverride') || '',
  repoSettingsBaseline: '',
  repoSettingsSaving: false,
  repoSettingsSavedAt: 0,
};

function preferredProjectFromList(projects) {
  if (!Array.isArray(projects) || projects.length === 0) return null;
  const sorted = [...projects].sort((left, right) => {
    const queueDelta = Number(right.queue_depth || 0) - Number(left.queue_depth || 0);
    if (queueDelta !== 0) return queueDelta;
    return String(left.name || '').localeCompare(String(right.name || ''));
  });
  return sorted[0]?.id || null;
}

const appShell = document.getElementById('app-shell');
const content = document.querySelector('.content');
const sidebarToggle = document.getElementById('sidebar-toggle');
const sidebarToggleLabel = document.getElementById('sidebar-toggle-label');
const projectSelect = document.getElementById('project-select');
const bannerProjectSelect = document.getElementById('banner-project-select');
const objectiveList = document.getElementById('objective-list');
const objectiveTitle = document.getElementById('objective-title');
const objectiveSummary = document.getElementById('objective-summary');
const objectiveBanner = document.getElementById('objective-banner');
const objectiveBannerTitle = document.getElementById('objective-banner-title');
const objectiveBannerMeta = document.getElementById('objective-banner-meta');
const atomicObjectiveSelect = document.getElementById('atomic-objective-select');
const objectiveGate = document.getElementById('objective-gate');
const objectiveGateSection = document.getElementById('objective-gate-section');
const nextActionTitle = document.getElementById('next-action-title');
const nextActionBody = document.getElementById('next-action-body');
const expectationRole = document.getElementById('expectation-role');
const expectationNeed = document.getElementById('expectation-need');
const expectationWhy = document.getElementById('expectation-why');
const expectationDone = document.getElementById('expectation-done');
const nextActionPanel = document.getElementById('next-action-panel');
const nextActionSaved = document.getElementById('next-action-saved');
const conversationTranscript = document.getElementById('conversation-transcript');
const conversationForm = document.getElementById('conversation-form');
const conversationInput = document.getElementById('conversation-input');
const conversationSubmit = document.getElementById('conversation-submit');
const conversationInterrupt = document.getElementById('conversation-interrupt');
const conversationPrimaryActions = document.getElementById('conversation-primary-actions');
const conversationPrimaryButton = document.getElementById('conversation-primary-button');
const diagramCommentAnchor = document.getElementById('diagram-comment-anchor');
const diagramCommentAnchorLabel = document.getElementById('diagram-comment-anchor-label');
const diagramCommentAnchorClear = document.getElementById('diagram-comment-anchor-clear');
const proposalActions = document.getElementById('proposal-actions');
const inlineOutputPanel = document.getElementById('inline-output-panel');
const inlineOutputSummary = document.getElementById('inline-output-summary');
const inlineOutputSummaryBody = document.getElementById('inline-output-summary-body');
const inlineOutputToggle = document.getElementById('inline-output-toggle');
const inlineOutputTabs = document.getElementById('inline-output-tabs');
const inlineOutputBody = document.getElementById('inline-output-body');
const stepBack = document.getElementById('step-back');
const stepExpand = document.getElementById('step-expand');
const contentGrid = document.getElementById('content-grid');
const objectivePanel = document.getElementById('objective-panel');
const stepPrompt = document.getElementById('step-prompt');
const stepQuestion = document.getElementById('step-question');
const stepHelper = document.getElementById('step-helper');
const intentSaveButton = document.getElementById('intent-save-button');
const mermaidPanel = document.getElementById('mermaid-panel');
const mermaidStepPrompt = document.getElementById('mermaid-step-prompt');
const mermaidStepQuestion = document.getElementById('mermaid-step-question');
const mermaidStepHelper = document.getElementById('mermaid-step-helper');
const mermaidProposalSummary = document.getElementById('mermaid-proposal-summary');
const interrogationPanel = document.getElementById('interrogation-panel');
const interrogationSummary = document.getElementById('interrogation-summary');
const interrogationQuestions = document.getElementById('interrogation-questions');
const interrogationPlan = document.getElementById('interrogation-plan');
const interrogationCompleteButton = document.getElementById('interrogation-complete-button');
const executionPanel = document.getElementById('execution-panel');
const atomicPanel = document.getElementById('atomic-panel');
const atomicTitle = document.getElementById('atomic-title');
const atomicSummary = document.getElementById('atomic-summary');
const atomicGenerationStatus = document.getElementById('atomic-generation-status');
const atomicGenerationMeta = document.getElementById('atomic-generation-meta');
const atomicList = document.getElementById('atomic-list');
const executionTitle = document.getElementById('execution-title');
const executionObjective = document.getElementById('execution-objective');
const executionTaskMeta = document.getElementById('execution-task-meta');
const executionRunMeta = document.getElementById('execution-run-meta');
const executionExplanation = document.getElementById('execution-explanation');
const executionPrimaryButton = document.getElementById('execution-primary-button');
const cliPanel = document.getElementById('cli-panel');
const mermaidMeta = document.getElementById('mermaid-meta');
const mermaidControls = document.getElementById('mermaid-controls');
const createObjectiveForm = document.getElementById('create-objective-form');
const createObjectiveTitle = document.getElementById('create-objective-title');
const createObjectiveSummary = document.getElementById('create-objective-summary');
const pageCreateObjectiveForm = document.getElementById('page-create-objective-form');
const pageCreateObjectiveProject = document.getElementById('page-create-objective-project');
const pageCreateObjectiveTitle = document.getElementById('page-create-objective-title');
const pageCreateObjectiveSummary = document.getElementById('page-create-objective-summary');
const intentSummary = document.getElementById('intent-summary');
const successDefinition = document.getElementById('success-definition');
const nonNegotiables = document.getElementById('non-negotiables');
const frustrationSignals = document.getElementById('frustration-signals');
const intentForm = document.getElementById('intent-form');
const taskList = document.getElementById('task-list');
const runList = document.getElementById('run-list');
const workspaceTitle = document.getElementById('workspace-title');
const workspaceSummary = document.getElementById('workspace-summary');
const workspaceStatus = document.getElementById('workspace-status');
const viewNav = document.getElementById('view-nav');
const headerCreateObjective = document.getElementById('header-create-objective');
const diagramShell = document.getElementById('diagram-shell');
const outputTabs = document.getElementById('output-tabs');
const outputBody = document.getElementById('output-body');
const pageError = document.getElementById('page-error');
const promotionReviewPanel = document.getElementById('promotion-review-panel');
const promotionReviewTitle = document.getElementById('promotion-review-title');
const promotionReviewSummary = document.getElementById('promotion-review-summary');
const promotionReviewMeta = document.getElementById('promotion-review-meta');
const promotionReviewContent = document.getElementById('promotion-review-content');
const promotionReviewRoundsPanel = document.getElementById('promotion-review-rounds-panel');
const promotionReviewRoundsContent = document.getElementById('promotion-review-rounds-content');
const newObjectivePanel = document.getElementById('new-objective-panel');
const tokenPerformancePanel = document.getElementById('token-performance-panel');
const tokenPerformanceContent = document.getElementById('token-performance-content');
const settingsPanel = document.getElementById('settings-panel');
const settingsContent = document.getElementById('settings-content');
const modalOverlay = document.getElementById('modal-overlay');
const modalCard = document.getElementById('modal-card');
const modalTitle = document.getElementById('modal-title');
const modalBody = document.getElementById('modal-body');
const modalStatusRow = document.getElementById('modal-status-row');
const modalStatusText = document.getElementById('modal-status-text');
const modalCancel = document.getElementById('modal-cancel');
const modalConfirm = document.getElementById('modal-confirm');
let activeConversationController = null;
let selectedDiagramElement = null;
let modalResolver = null;
let modalLocked = false;

function showError(message) {
  pageError.textContent = message;
  pageError.hidden = false;
}

function setConversationPending(value) {
  state.conversationPending = value;
  conversationSubmit.disabled = value;
  conversationSubmit.textContent = value ? 'Sending…' : 'Send';
  if (conversationInterrupt) {
    conversationInterrupt.hidden = !value;
  }
}

function setDiagramUpdating(value) {
  state.diagramUpdating = value;
  diagramShell.classList.toggle('updating', value);
}

function applyDiagramTransform() {
  const svg = diagramShell.querySelector('svg');
  if (!svg) return;
  svg.style.transformOrigin = '0 0';
  svg.style.transform = `translate(${state.diagramPan.x}px, ${state.diagramPan.y}px) scale(${state.diagramPan.scale})`;
  diagramShell.classList.toggle('panning', state.diagramPan.isDragging);
}

function resetDiagramPan() {
  state.diagramPan = { scale: 1, x: 0, y: 0, isPointerDown: false, isDragging: false, startX: 0, startY: 0, dragOriginX: 0, dragOriginY: 0 };
}

function clearDiagramAnchor() {
  state.diagramAnchor = null;
  if (selectedDiagramElement) {
    selectedDiagramElement.classList.remove('diagram-selected');
    selectedDiagramElement = null;
  }
  if (diagramCommentAnchor) {
    diagramCommentAnchor.classList.remove('visible');
  }
}

function setDiagramAnchor(anchor, element) {
  state.diagramAnchor = anchor;
  if (selectedDiagramElement && selectedDiagramElement !== element) {
    selectedDiagramElement.classList.remove('diagram-selected');
  }
  selectedDiagramElement = element || null;
  if (selectedDiagramElement) {
    selectedDiagramElement.classList.add('diagram-selected');
  }
  if (diagramCommentAnchor && diagramCommentAnchorLabel) {
    diagramCommentAnchorLabel.textContent = anchor
      ? `${anchor.label}`
      : '';
    diagramCommentAnchor.classList.toggle('visible', Boolean(anchor));
  }
}

function addLocalNotice(text) {
  if (!text || !state.objectiveId) return;
  state.localNotices.push({
    role: 'system',
    text,
    created_at: new Date().toISOString(),
    label: 'System',
    objective_id: state.objectiveId,
  });
}

function clearError() {
  pageError.hidden = true;
  pageError.textContent = '';
}

function renderModalState({
  title = '',
  body = '',
  confirmLabel = 'OK',
  cancelLabel = 'Cancel',
  tone = '',
  showCancel = true,
  locked = false,
  statusText = '',
} = {}) {
  modalLocked = locked;
  if (modalTitle) modalTitle.textContent = title || '';
  if (modalBody) modalBody.textContent = body || '';
  if (modalCard) modalCard.className = `modal-card ${tone}`.trim();
  if (modalStatusText) modalStatusText.textContent = statusText || '';
  if (modalStatusRow) modalStatusRow.hidden = !statusText;
  if (modalConfirm) {
    modalConfirm.textContent = confirmLabel;
    modalConfirm.disabled = locked;
  }
  if (modalCancel) {
    modalCancel.textContent = cancelLabel;
    modalCancel.hidden = !showCancel;
    modalCancel.disabled = locked;
  }
}

function openModal({ title, body, confirmLabel = 'OK', cancelLabel = 'Cancel', tone = '', showCancel = true }) {
  return new Promise((resolve) => {
    modalResolver = resolve;
    renderModalState({ title, body, confirmLabel, cancelLabel, tone, showCancel, locked: false, statusText: '' });
    if (modalOverlay) modalOverlay.hidden = false;
  });
}

function setModalWorking({ title, body, statusText = 'Working…' }) {
  renderModalState({
    title,
    body,
    confirmLabel: 'Working…',
    cancelLabel: 'Cancel',
    tone: 'working',
    showCancel: false,
    locked: true,
    statusText,
  });
  if (modalOverlay) modalOverlay.hidden = false;
}

function closeModal(result) {
  modalLocked = false;
  if (modalOverlay) modalOverlay.hidden = true;
  const resolver = modalResolver;
  modalResolver = null;
  if (resolver) resolver(result);
}

function repoSettingsSignature(payload) {
  return JSON.stringify({
    promotion_mode: String(payload?.promotion_mode || ''),
    repo_provider: String(payload?.repo_provider || ''),
    repo_name: String(payload?.repo_name || ''),
    base_branch: String(payload?.base_branch || ''),
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ error: `Request failed: ${response.status}` }));
    throw new Error(payload.error || `Request failed: ${response.status}`);
  }
  return response.json();
}

function pickDefaultRun(workspace) {
  if (!workspace) return null;
  const task = workspace.tasks.find((item) => item.id === state.taskId) || workspace.tasks[0];
  if (!task) return null;
  const latestRun = [...task.runs].reverse()[0];
  return latestRun ? latestRun.id : null;
}

function currentObjective() {
  return state.workspace?.objectives.find((item) => item.id === state.objectiveId) || null;
}

function currentIntentModel() {
  return currentObjective()?.intent_model || null;
}

function likelyMermaidActionIntent(text) {
  const lowered = String(text || '').trim().toLowerCase();
  if (!lowered) return false;
  const directMermaid = ['mermaid', 'diagram', 'control flow', 'flowchart', 'flow chart'].some((term) => lowered.includes(term));
  const updateVerb = ['update', 'revise', 'regenerate', 'rewrite', 'change', 'remove', 'add'].some((term) => lowered.includes(term));
  if (directMermaid && updateVerb) return true;
  const objective = currentObjective();
  const mode = objective ? currentFocusMode(objective) : '';
  const proposalPending = Boolean(objective?.diagram_proposal);
  if (mode === 'mermaid_review' && proposalPending && updateVerb) {
    return true;
  }
  if (mode === 'mermaid_review' && state.diagramAnchor && !/(looks good|i like this|keep this|leave this|approve|matches my flow)/.test(lowered)) {
    return true;
  }
  if (mode === 'mermaid_review' && updateVerb && ['step', 'loop', 'gate', 'branch', 'path', 'node', 'box', 'label', 'exit condition'].some((term) => lowered.includes(term))) {
    return true;
  }
  const shortImperative = ['do it', 'do it.', 'make the changes', 'make your changes', 'apply it', 'update it', 'do that', 'make it so', 'go ahead'].includes(lowered);
  if (!shortImperative) return false;
  const transcriptText = conversationTranscript.textContent.toLowerCase();
  const recentHarness = (currentObjective()?.diagram_proposal ? 'proposal pending ' : '') + transcriptText;
  return recentHarness.includes('update the mermaid')
    || recentHarness.includes('revise that diagram')
    || recentHarness.includes('proposed mermaid update')
    || recentHarness.includes('generated a proposed mermaid update')
    || recentHarness.includes('the diagram should be revised')
    || recentHarness.includes('make your changes to the diagram');
}

function applySidebarState() {
  appShell.classList.toggle('sidebar-collapsed', state.sidebarCollapsed);
  sidebarToggleLabel.textContent = state.sidebarCollapsed ? '>' : '<';
}

function setSidebarCollapsed(value) {
  state.sidebarCollapsed = value;
  localStorage.setItem('accruvia.ui.sidebarCollapsed', value ? '1' : '0');
  applySidebarState();
}

function setProjectId(value) {
  state.projectId = value;
  if (value) {
    localStorage.setItem('accruvia.ui.projectId', value);
  } else {
    localStorage.removeItem('accruvia.ui.projectId');
  }
}

function setObjectiveId(value) {
  if (state.objectiveId && value && state.objectiveId !== value) {
    state.manualViewOverride = '';
    sessionStorage.removeItem('accruvia.ui.manualViewOverride');
  }
  state.objectiveId = value;
  if (value) {
    localStorage.setItem('accruvia.ui.objectiveId', value);
  } else {
    localStorage.removeItem('accruvia.ui.objectiveId');
  }
}

function currentRecommendedView() {
  const objective = currentObjective();
  if (!objective) return '';
  return objective.recommended_view || '';
}

function shouldAutoFollowRecommendedView() {
  const recommended = currentRecommendedView();
  if (!recommended || !['atomic', 'promotion-review'].includes(recommended)) return false;
  if (!['atomic', 'promotion-review'].includes(state.view)) return false;
  if (!state.manualViewOverride) return true;
  return state.manualViewOverride === recommended;
}

function maybeFollowRecommendedView() {
  const recommended = currentRecommendedView();
  if (!recommended || recommended === state.view) return;
  if (!shouldAutoFollowRecommendedView()) return;
  const params = new URLSearchParams();
  if (state.projectId) params.set('project_id', state.projectId);
  if (state.objectiveId) params.set('objective_id', state.objectiveId);
  const suffix = params.toString() ? `?${params.toString()}` : '';
  const target = recommended === 'promotion-review'
    ? `/promotion-review${suffix}`
    : `/atomic${suffix}`;
  window.location.assign(target);
}

function setExpandAll(value) {
  state.expandAll = value;
  localStorage.setItem('accruvia.ui.expandAll', value ? '1' : '0');
}

function humanTaskTitle(task) {
  if (!task) return '';
  return String(task.title || '').replace(/^First slice:\\s*/i, 'Implementation step: ');
}

function formatTimestamp(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString();
}

function formatRelativeTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const diffSeconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (diffSeconds < 60) return `${diffSeconds} sec${diffSeconds === 1 ? '' : 's'} ago`;
  const diffMinutes = Math.floor(diffSeconds / 60);
  if (diffMinutes < 60) return `${diffMinutes} min${diffMinutes === 1 ? '' : 's'} ago`;
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours} hour${diffHours === 1 ? '' : 's'} ago`;
  const diffDays = Math.floor(diffHours / 24);
  return `${diffDays} day${diffDays === 1 ? '' : 's'} ago`;
}

function currentMermaidTimestamp(objective) {
  if (!objective) return '';
  if (objective.diagram_proposal?.created_at) return objective.diagram_proposal.created_at;
  return objective.diagram?.updated_at || objective.diagram?.created_at || '';
}

function renderMermaidMeta(objective) {
  if (!objective || !objective.diagram) {
    mermaidMeta.textContent = 'No Mermaid artifact yet.';
    return;
  }
  const suffix = objective.diagram.required_for_execution ? ' · required' : '';
  const timestamp = currentMermaidTimestamp(objective);
  const stamp = formatTimestamp(timestamp);
  const relative = formatRelativeTime(timestamp);
  if (objective.diagram_proposal) {
    mermaidMeta.textContent = `${objective.diagram.diagram_type} · proposal pending · current v${objective.diagram.version}${suffix} · Last updated at ${stamp}${relative ? ` (${relative})` : ''}`;
    return;
  }
  const locked = objective.diagram.status === 'finished' ? ' · locked' : '';
  mermaidMeta.textContent = `${objective.diagram.diagram_type} · v${objective.diagram.version} · ${objective.diagram.status}${locked}${suffix} · Last updated at ${stamp}${relative ? ` (${relative})` : ''}`;
}

function currentFocusMode(objective) {
  if (state.manualFocusMode) return state.manualFocusMode;
  if (!objective) return 'empty';
  const model = objective.intent_model || null;
  if (!model || !(model.intent_summary || '').trim()) {
    return 'desired_outcome';
  }
  if (!(model.success_definition || '').trim()) {
    return 'success_definition';
  }
  if (!((model.non_negotiables || []).length)) {
    return 'non_negotiables';
  }
  if (!(objective.interrogation_review?.completed)) {
    return 'interrogation_review';
  }
  const checks = objective.execution_gate?.checks || [];
  const mermaidBlocked = checks.find((check) => check.key === 'mermaid_finished' && !check.ok);
  if (mermaidBlocked) {
    return 'mermaid_review';
  }
  const linkedTasks = (state.workspace?.tasks || []).filter((task) => task.objective_id === objective.id);
  if (linkedTasks.length === 0) {
    return 'run_start';
  }
  const latestRun = [...(linkedTasks[0]?.runs || [])].reverse()[0] || null;
  if (!latestRun) {
    return 'run_start';
  }
  return 'run_review';
}

function previousFocusMode(objective) {
  const mode = currentFocusMode(objective);
  if (mode === 'success_definition') return 'desired_outcome';
  if (mode === 'non_negotiables') return 'success_definition';
  if (mode === 'interrogation_review') return 'non_negotiables';
  if (mode === 'mermaid_review') return 'interrogation_review';
  if (mode === 'run_start' || mode === 'run_review') return 'mermaid_review';
  return mode;
}

function applyFocusMode(objective) {
  const mode = currentFocusMode(objective);
  if (state.view === 'control-flow' || state.view === 'atomic') {
    state.expandAll = false;
  }
  const focused = !state.expandAll && mode !== 'empty';
  const mermaidWorkspace = focused && mode === 'mermaid_review';
  content.classList.toggle('mode-mermaid-review', mermaidWorkspace);
  contentGrid.classList.toggle('focused', focused);
  contentGrid.classList.toggle('hidden', false);
  nextActionPanel.hidden = !state.expandAll && !['desired_outcome', 'success_definition', 'non_negotiables', 'interrogation_review', 'mermaid_review', 'run_start', 'run_review'].includes(mode);
  objectivePanel.hidden = !state.expandAll || ['mermaid_review', 'run_start', 'run_review'].includes(mode);
  interrogationPanel.hidden = true;
  mermaidPanel.hidden = !(state.expandAll || mode === 'mermaid_review');
  if (atomicPanel) {
    atomicPanel.hidden = state.view !== 'atomic';
  }
  executionPanel.hidden = !(state.expandAll || ['run_start', 'run_review'].includes(mode)) || mode === 'mermaid_review';
  cliPanel.hidden = !(state.expandAll || mode === 'run_review');
  inlineOutputPanel.hidden = !(
    mode === 'run_review' &&
    state.showInlineReview &&
    state.runOutput &&
    (
      state.runOutput.summary ||
      ((state.runOutput.sections || []).length > 0 && state.activeSectionPath)
    )
  );

  intentSummary.hidden = !state.expandAll && mode !== 'desired_outcome';
  successDefinition.hidden = !state.expandAll && mode !== 'success_definition';
  nonNegotiables.hidden = !state.expandAll && mode !== 'non_negotiables';
  frustrationSignals.hidden = !state.expandAll;
  objectiveGate.hidden = !state.expandAll;
  objectiveGateSection.hidden = !state.expandAll;
  nextActionSaved.hidden = !state.expandAll && !['desired_outcome', 'success_definition', 'non_negotiables', 'interrogation_review'].includes(mode);
  conversationForm.hidden = false;
  conversationPrimaryActions.hidden = !['run_start', 'run_review'].includes(mode) || mode === 'mermaid_review';
  if (state.view === 'atomic') {
    nextActionPanel.hidden = false;
    objectivePanel.hidden = true;
    interrogationPanel.hidden = true;
    mermaidPanel.hidden = true;
    executionPanel.hidden = true;
    cliPanel.hidden = true;
    conversationPrimaryActions.hidden = true;
    proposalActions.hidden = true;
  }

  stepBack.hidden = state.expandAll || ['empty', 'desired_outcome'].includes(mode);
  stepExpand.textContent = state.expandAll ? 'Focus next step' : 'Show everything';
  const prompt = promptForMode(mode);
  const promptInMermaid = mode === 'mermaid_review' && !state.expandAll;
  stepPrompt.hidden = true;
  mermaidStepPrompt.hidden = !promptInMermaid;
  mermaidProposalSummary.hidden = true;
  stepQuestion.textContent = prompt.question;
  stepHelper.textContent = prompt.helper;
  mermaidStepQuestion.textContent = prompt.question;
  mermaidStepHelper.textContent = prompt.helper;
  intentSaveButton.textContent = 'Send';
  if (!conversationForm.hidden) {
    conversationSubmit.textContent = state.conversationPending
      ? 'Sending…'
      : 'Send';
    conversationInput.placeholder = ['run_start', 'run_review'].includes(mode)
      ? 'Ask what happened, what to do next, or tell the harness what you want changed.'
      : (prompt.question || 'Ask the harness a question, push back, or clarify what you want here.');
    conversationInput.classList.toggle('stage-compact', ['run_start', 'run_review', 'mermaid_review'].includes(mode));
    conversationSubmit.disabled = state.conversationPending;
  }

  if (mode === 'desired_outcome') {
    if (!state.suppressFocusAnimation) {
      animateStepFocus(nextActionPanel, conversationInput);
    }
    conversationInput.value = currentIntentModel()?.intent_summary || '';
    conversationInput.focus();
  } else if (mode === 'success_definition') {
    if (!state.suppressFocusAnimation) {
      animateStepFocus(nextActionPanel, conversationInput);
    }
    conversationInput.value = currentIntentModel()?.success_definition || '';
    conversationInput.focus();
  } else if (mode === 'non_negotiables') {
    if (!state.suppressFocusAnimation) {
      animateStepFocus(nextActionPanel, conversationInput);
    }
    conversationInput.value = (currentIntentModel()?.non_negotiables || []).join('\\n');
    conversationInput.focus();
  } else if (mode === 'interrogation_review') {
    if (!state.suppressFocusAnimation) {
      animateStepFocus(nextActionPanel, conversationTranscript);
    }
    if (!conversationInput.value.trim()) {
      conversationInput.value = '';
    }
    conversationInput.focus();
  } else if (mode === 'mermaid_review') {
    if (!state.suppressFocusAnimation) {
      window.scrollTo({top: 0, behavior: 'smooth'});
      animateStepFocus(mermaidPanel, mermaidPanel);
    }
    if (!conversationInput.value.trim()) {
      conversationInput.value = '';
    }
    conversationInput.focus();
  } else if (mode === 'run_start' || mode === 'run_review') {
    if (!state.suppressFocusAnimation) {
      animateStepFocus(mode === 'run_review' && state.showInlineReview && !inlineOutputPanel.hidden ? inlineOutputPanel : executionPanel, mode === 'run_review' && state.showInlineReview && !inlineOutputPanel.hidden ? inlineOutputPanel : executionPanel);
    }
    if (!conversationInput.value.trim()) {
      conversationInput.value = '';
    }
  }
}

function promptForMode(mode) {
  if (mode === 'desired_outcome') {
    return {
      question: 'What do you want this objective to achieve?',
      helper: 'Describe the result you want, not the implementation steps.',
    };
  }
  if (mode === 'success_definition') {
    return {
      question: 'How will you know this objective is actually done?',
      helper: 'Describe what success looks like from your perspective so the system can measure it later.',
    };
  }
  if (mode === 'non_negotiables') {
    return {
      question: 'What must the solution not violate?',
      helper: 'List hard constraints, expectations, or solution-shape requirements one per line.',
    };
  }
  if (mode === 'interrogation_review') {
    return {
      question: 'Answer the red-team questions before process review.',
      helper: 'The harness should interrogate the objective and challenge the initial plan before Mermaid review.',
    };
  }
  if (mode === 'mermaid_review') {
    return {
      question: 'Does this process diagram match your intended flow?',
      helper: 'Finish it if the flow is good enough to govern execution. Pause it if the process is still unclear.',
    };
  }
  return {question: '', helper: ''};
}

function animateStepFocus(promptElement, targetElement) {
  promptElement.classList.remove('pulse');
  targetElement.classList.remove('pulse', 'step-target');
  void promptElement.offsetWidth;
  void targetElement.offsetWidth;
  promptElement.classList.add('pulse');
  targetElement.classList.add('step-target', 'pulse');
  targetElement.scrollIntoView({behavior: 'smooth', block: 'start'});
  setTimeout(() => {
    promptElement.classList.remove('pulse');
    targetElement.classList.remove('pulse');
  }, 950);
}

function renderSavedAnswers(objective) {
  const model = objective?.intent_model || null;
  if (!model) {
    nextActionSaved.innerHTML = '';
    return;
  }
  const blocks = [];
  if ((model.intent_summary || '').trim()) {
    blocks.push(`<div class="saved-answer" data-step="desired_outcome"><div class="meta">Saved desired outcome${state.lastSavedStep === 'desired_outcome' ? ' just now' : ''}</div><div>${escapeHtml(model.intent_summary)}</div></div>`);
  }
  if ((model.success_definition || '').trim()) {
    blocks.push(`<div class="saved-answer" data-step="success_definition"><div class="meta">Saved success definition${state.lastSavedStep === 'success_definition' ? ' just now' : ''}</div><div>${escapeHtml(model.success_definition)}</div></div>`);
  }
  if ((model.non_negotiables || []).length) {
    blocks.push(`<div class="saved-answer" data-step="non_negotiables"><div class="meta">Saved non-negotiables${state.lastSavedStep === 'non_negotiables' ? ' just now' : ''}</div><div>${escapeHtml(model.non_negotiables.join(', '))}</div></div>`);
  }
  if (objective?.interrogation_review?.completed) {
    blocks.push(`<div class="saved-answer" data-step="interrogation_review"><div class="meta">Interrogation completed</div><div>${escapeHtml(objective.interrogation_review.summary || 'The harness completed an interrogation and self-red-team pass before Mermaid review.')}</div></div>`);
  }
  nextActionSaved.innerHTML = blocks.join('');
}

function renderConversationTranscript(objective) {
  if (!objective) {
    conversationTranscript.innerHTML = '';
    return;
  }
  const comments = (state.workspace?.comments || [])
    .filter((comment) => comment.objective_id === objective.id)
    .map((comment) => ({
      role: 'operator',
      text: comment.text,
      created_at: comment.created_at,
      label: comment.author || 'You',
    }));
  const replies = (state.workspace?.replies || [])
    .filter((reply) => reply.objective_id === objective.id)
    .map((reply) => ({
      role: 'harness',
      text: reply.text,
      created_at: reply.created_at,
      label: 'Harness',
    }));
  const receipts = (state.workspace?.action_receipts || [])
    .filter((receipt) => receipt.objective_id === objective.id)
    .map((receipt) => ({
      role: 'system',
      text: receipt.text,
      created_at: receipt.created_at,
      label: 'System',
    }));
  const notices = (state.localNotices || [])
    .filter((notice) => notice.objective_id === objective.id)
    .map((notice) => ({
      role: notice.role || 'system',
      text: notice.text,
      created_at: notice.created_at,
      label: notice.label || 'System',
    }));
  const transcript = [...comments, ...replies, ...receipts, ...notices]
    .sort((left, right) => String(left.created_at).localeCompare(String(right.created_at)))
    .slice(-10);
  const bubbles = [];
  if (currentFocusMode(objective) === 'interrogation_review') {
    const review = objective.interrogation_review || { questions: [] };
    const relevantAnswers = comments.filter((comment) => {
      const createdAt = objective.intent_model?.created_at || '';
      return !createdAt || String(comment.created_at) >= String(createdAt);
    });
    const questionIndex = Math.min(relevantAnswers.length, Math.max(0, (review.questions || []).length - 1));
    const prompt = (review.questions || [])[questionIndex] || 'What ambiguity should be resolved before Mermaid review?';
    bubbles.push(
      `<div class="transcript-bubble harness"><div class="meta">Harness</div><div><strong>Interrogation question</strong></div><div style="margin-top:0.35rem;">${escapeHtml(prompt)}</div></div>`
    );
    if (transcript.length > 0) {
      for (const item of transcript.slice(-6)) {
        bubbles.push(
          `<div class="transcript-bubble ${item.role === 'operator' ? 'operator' : item.role === 'system' ? 'system' : 'harness'}"><div class="meta">${escapeHtml(item.label)}${item.created_at ? ` · ${formatRelativeTime(item.created_at)}` : ''}</div><div>${escapeHtml(item.text)}</div></div>`
        );
      }
    }
  } else if (transcript.length === 0) {
    const nextAction = describeNextAction(objective);
    bubbles.push(
      `<div class="transcript-bubble harness"><div class="meta">Harness</div><div><strong>${escapeHtml(nextAction.title)}</strong></div><div style="margin-top:0.35rem;">${escapeHtml(nextAction.body)}</div></div>`
    );
  } else {
    for (const item of transcript) {
      bubbles.push(
        `<div class="transcript-bubble ${item.role === 'operator' ? 'operator' : item.role === 'system' ? 'system' : 'harness'}"><div class="meta">${escapeHtml(item.label)}${item.created_at ? ` · ${formatRelativeTime(item.created_at)}` : ''}</div><div>${escapeHtml(item.text)}</div></div>`
      );
    }
  }
  conversationTranscript.innerHTML = bubbles.join('');
  conversationTranscript.scrollTop = conversationTranscript.scrollHeight;
}

function findDiagramAnchorTarget(start) {
  let node = start;
  while (node && node !== diagramShell) {
    if (!(node instanceof SVGElement)) {
      node = node.parentNode;
      continue;
    }
    const classes = Array.from(node.classList || []);
    if (
      classes.includes('node')
      || classes.includes('edgeLabel')
      || classes.includes('label')
      || classes.includes('cluster')
      || node.tagName === 'foreignObject'
      || node.tagName === 'text'
      || node.tagName === 'tspan'
    ) {
      return node;
    }
    node = node.parentNode;
  }
  return null;
}

function isClickableMermaidTarget(start) {
  return Boolean(findDiagramAnchorTarget(start));
}

function anchorFromElement(element) {
  if (!element) return null;
  const root = element.closest('.node, .edgeLabel, .label, .cluster') || element;
  const raw = (root.textContent || '').replace(/\s+/g, ' ').trim();
  const label = raw.slice(0, 140) || 'Selected diagram area';
  const svg = diagramShell.querySelector('svg');
  let position = '';
  if (svg && root.getBoundingClientRect) {
    const svgRect = svg.getBoundingClientRect();
    const rect = root.getBoundingClientRect();
    if (svgRect.width > 0 && svgRect.height > 0) {
      const x = Math.round(((rect.left + rect.width / 2 - svgRect.left) / svgRect.width) * 100);
      const y = Math.round(((rect.top + rect.height / 2 - svgRect.top) / svgRect.height) * 100);
      position = `${x}%, ${y}%`;
    }
  }
  return { label, position };
}

function renderProjects() {
  const selectors = [projectSelect, bannerProjectSelect, pageCreateObjectiveProject].filter(Boolean);
  for (const selector of selectors) {
    selector.innerHTML = '';
    for (const project of state.projects) {
      const option = document.createElement('option');
      option.value = project.id;
      option.textContent = `${project.name} (${project.id})`;
      option.selected = project.id === state.projectId;
      selector.appendChild(option);
    }
  }
}

function renderObjectiveCreatePage() {
  if (!newObjectivePanel) return;
  newObjectivePanel.hidden = state.view !== 'objective-create';
  if (state.view !== 'objective-create') return;
  if (pageCreateObjectiveProject && state.projectId) {
    pageCreateObjectiveProject.value = state.projectId;
  }
  const cancelLink = document.querySelector('.objective-create-cancel');
  if (cancelLink instanceof HTMLAnchorElement) {
    const params = new URLSearchParams();
    if (state.projectId) params.set('project_id', state.projectId);
    const suffix = params.toString() ? `?${params.toString()}` : '';
    cancelLink.href = `/workspace${suffix}`;
  }
}

function renderTokenPerformancePage() {
  if (!tokenPerformancePanel || !tokenPerformanceContent) return;
  tokenPerformancePanel.hidden = state.view !== 'token-performance';
  if (state.view !== 'token-performance') return;
  const workspace = state.workspace;
  if (!workspace) {
    tokenPerformanceContent.innerHTML = '<div class="empty">No workspace loaded.</div>';
    return;
  }
  const objectiveRows = [];
  const reviewerRows = new Map();
  const roundRows = [];
  const totals = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0, latency_ms: 0, packet_count: 0, round_count: 0, reported_packet_count: 0, unreported_packet_count: 0 };
  function addUsage(target, usage, packetCount = 0, roundCount = 0) {
    target.prompt_tokens += Number(usage.prompt_tokens || 0);
    target.completion_tokens += Number(usage.completion_tokens || 0);
    target.total_tokens += Number(usage.total_tokens || 0);
    target.cost_usd += Number(usage.cost_usd || 0);
    target.latency_ms += Number(usage.latency_ms || 0);
    target.packet_count += packetCount;
    target.round_count += roundCount;
    target.reported_packet_count += Number(usage.reported_packet_count || 0);
    target.unreported_packet_count += Number(usage.unreported_packet_count || 0);
  }
  function summarizePackets(packetList) {
    const usage = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0, latency_ms: 0, reported_packet_count: 0, unreported_packet_count: 0 };
    for (const packet of Array.isArray(packetList) ? packetList : []) {
      const llmUsage = packet?.llm_usage && typeof packet.llm_usage === 'object' ? packet.llm_usage : {};
      const reported = packet?.llm_usage_reported !== false;
      if (reported) {
        usage.prompt_tokens += Number(llmUsage.prompt_tokens || 0);
        usage.completion_tokens += Number(llmUsage.completion_tokens || 0);
        usage.total_tokens += Number(llmUsage.total_tokens || 0);
        usage.cost_usd += Number(llmUsage.cost_usd || 0);
        usage.latency_ms += Number(llmUsage.latency_ms || 0);
        usage.reported_packet_count += 1;
      } else {
        usage.unreported_packet_count += 1;
        usage.latency_ms += Number(llmUsage.latency_ms || 0);
      }
    }
    return usage;
  }
  function fmtTokens(value) {
    return Number(value || 0).toLocaleString();
  }
  function fmtCost(value) {
    return `$${Number(value || 0).toFixed(4)}`;
  }
  function fmtLatency(value) {
    const ms = Number(value || 0);
    return ms > 0 ? `${Math.round(ms)}ms` : '0ms';
  }
  for (const objective of workspace.objectives || []) {
    const review = objective.promotion_review || {};
    const rounds = Array.isArray(review.review_rounds) ? review.review_rounds : [];
    if (!rounds.length) continue;
    const objectiveUsage = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0, latency_ms: 0, packet_count: 0, round_count: 0 };
    for (const round of rounds) {
      const packets = Array.isArray(round.packets) ? round.packets : [];
      const roundUsage = summarizePackets(packets);
      addUsage(objectiveUsage, roundUsage, packets.length, 1);
      addUsage(totals, roundUsage, packets.length, 1);
      roundRows.push({
        objective_title: objective.title,
        round_number: round.round_number,
        status: round.status,
        packet_count: packets.length,
        usage: roundUsage,
      });
      for (const packet of packets) {
        const reviewer = String(packet.reviewer || packet.dimension || 'unknown');
        const key = reviewer;
        const current = reviewerRows.get(key) || { reviewer, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0, latency_ms: 0, packet_count: 0, reported_packet_count: 0, unreported_packet_count: 0 };
        const llmUsage = packet.llm_usage && typeof packet.llm_usage === 'object' ? packet.llm_usage : {};
        if (packet.llm_usage_reported === false) {
          current.unreported_packet_count += 1;
          current.latency_ms += Number(llmUsage.latency_ms || 0);
        } else {
          current.prompt_tokens += Number(llmUsage.prompt_tokens || 0);
          current.completion_tokens += Number(llmUsage.completion_tokens || 0);
          current.total_tokens += Number(llmUsage.total_tokens || 0);
          current.cost_usd += Number(llmUsage.cost_usd || 0);
          current.latency_ms += Number(llmUsage.latency_ms || 0);
          current.reported_packet_count += 1;
        }
        current.packet_count += 1;
        reviewerRows.set(key, current);
      }
    }
    objectiveRows.push({
      title: objective.title,
      round_count: objectiveUsage.round_count,
      packet_count: objectiveUsage.packet_count,
      usage: objectiveUsage,
    });
  }
  objectiveRows.sort((left, right) => right.usage.total_tokens - left.usage.total_tokens);
  roundRows.sort((left, right) => right.usage.total_tokens - left.usage.total_tokens);
  const reviewerList = Array.from(reviewerRows.values()).sort((left, right) => right.total_tokens - left.total_tokens);
  const avgTokensPerRound = totals.round_count ? Math.round(totals.total_tokens / totals.round_count) : 0;
  const avgCostPerRound = totals.round_count ? totals.cost_usd / totals.round_count : 0;
  const avgTokensPerPacket = totals.packet_count ? Math.round(totals.total_tokens / totals.packet_count) : 0;
  const objectiveTable = objectiveRows.length
    ? `
      <table>
        <thead><tr><th>Objective</th><th>Rounds</th><th>Packets</th><th>Tokens</th><th>Cost</th><th>Latency</th></tr></thead>
        <tbody>
          ${objectiveRows.map((row) => `<tr><td>${escapeHtml(row.title)}</td><td>${row.round_count}</td><td>${row.packet_count}</td><td>${row.usage.reported_packet_count ? fmtTokens(row.usage.total_tokens) : 'Not reported'}</td><td>${row.usage.reported_packet_count ? fmtCost(row.usage.cost_usd) : 'Not reported'}</td><td>${fmtLatency(row.usage.latency_ms)}</td></tr>`).join('')}
        </tbody>
      </table>
    `
    : '<div class="empty">No review token usage has been recorded yet.</div>';
  const reviewerTable = reviewerList.length
    ? `
      <table>
        <thead><tr><th>Reviewer</th><th>Packets</th><th>Tokens</th><th>Cost</th><th>Latency</th></tr></thead>
        <tbody>
          ${reviewerList.map((row) => `<tr><td>${escapeHtml(row.reviewer)}</td><td>${row.packet_count}</td><td>${row.reported_packet_count ? fmtTokens(row.total_tokens) : 'Not reported'}</td><td>${row.reported_packet_count ? fmtCost(row.cost_usd) : 'Not reported'}</td><td>${fmtLatency(row.latency_ms)}</td></tr>`).join('')}
        </tbody>
      </table>
    `
    : '<div class="empty">No reviewer usage has been recorded yet.</div>';
  const roundTable = roundRows.length
    ? `
      <table>
        <thead><tr><th>Objective</th><th>Round</th><th>Status</th><th>Packets</th><th>Tokens</th><th>Cost</th></tr></thead>
        <tbody>
          ${roundRows.slice(0, 20).map((row) => `<tr><td>${escapeHtml(row.objective_title)}</td><td>${escapeHtml(String(row.round_number || '?'))}</td><td>${escapeHtml(String(row.status || 'unknown'))}</td><td>${row.packet_count}</td><td>${row.usage.reported_packet_count ? fmtTokens(row.usage.total_tokens) : 'Not reported'}</td><td>${row.usage.reported_packet_count ? fmtCost(row.usage.cost_usd) : 'Not reported'}</td></tr>`).join('')}
        </tbody>
      </table>
    `
    : '<div class="empty">No round usage has been recorded yet.</div>';
  tokenPerformanceContent.innerHTML = `
    <div class="token-performance-shell">
      <div class="token-performance-hero">
        <h3>Token performance</h3>
        <p>Track promotion-review token usage by objective, round, and reviewer so you can see where the board is spending time and money.</p>
      </div>
      <div class="token-performance-summary">
        <div class="token-performance-card"><div class="label">Total tokens</div><div class="value">${fmtTokens(totals.total_tokens)}</div></div>
        <div class="token-performance-card"><div class="label">Total cost</div><div class="value">${fmtCost(totals.cost_usd)}</div></div>
        <div class="token-performance-card"><div class="label">Review packets</div><div class="value">${fmtTokens(totals.packet_count)}</div></div>
        <div class="token-performance-card"><div class="label">Review rounds</div><div class="value">${fmtTokens(totals.round_count)}</div></div>
        <div class="token-performance-card"><div class="label">Reported packets</div><div class="value">${fmtTokens(totals.reported_packet_count)}</div></div>
        <div class="token-performance-card"><div class="label">Unreported packets</div><div class="value">${fmtTokens(totals.unreported_packet_count)}</div></div>
        <div class="token-performance-card"><div class="label">Avg tokens / round</div><div class="value">${fmtTokens(avgTokensPerRound)}</div></div>
        <div class="token-performance-card"><div class="label">Avg cost / round</div><div class="value">${fmtCost(avgCostPerRound)}</div></div>
        <div class="token-performance-card"><div class="label">Avg tokens / packet</div><div class="value">${fmtTokens(avgTokensPerPacket)}</div></div>
        <div class="token-performance-card"><div class="label">Total latency</div><div class="value">${fmtLatency(totals.latency_ms)}</div></div>
      </div>
      <div class="token-performance-grid">
        <section class="token-performance-table">
          <h4>By objective</h4>
          ${objectiveTable}
        </section>
        <section class="token-performance-table">
          <h4>By reviewer</h4>
          ${reviewerTable}
        </section>
        <section class="token-performance-table">
          <h4>Top review rounds</h4>
          ${roundTable}
        </section>
        <section class="token-performance-note">
          <h4>What belongs here next</h4>
          <p class="hint">This first pass uses objective-review packet usage because that telemetry is already persisted. The next useful additions are workflow-type splits, prompt-vs-completion ratios, interrupted/wasted rounds, and tokens per cleared concern.</p>
        </section>
      </div>
    </div>
  `;
}

function syncRepoSettingsButtonState() {
  const saveButton = document.getElementById('repo-settings-save-btn');
  const status = document.getElementById('repo-settings-save-status');
  const promotionMode = document.getElementById('settings-repo-promotion-mode');
  const repoProvider = document.getElementById('settings-repo-provider');
  const repoName = document.getElementById('settings-repo-name');
  const baseBranch = document.getElementById('settings-repo-base-branch');
  if (!(saveButton instanceof HTMLButtonElement) || !status) return;
  const currentSignature = repoSettingsSignature({
    promotion_mode: promotionMode?.value || '',
    repo_provider: repoProvider?.value || '',
    repo_name: repoName?.value || '',
    base_branch: baseBranch?.value || '',
  });
  const dirty = currentSignature !== state.repoSettingsBaseline;
  saveButton.classList.remove('is-saving', 'is-saved');
  if (state.repoSettingsSaving) {
    saveButton.disabled = true;
    saveButton.textContent = 'Saving…';
    saveButton.classList.add('is-saving');
    status.textContent = 'Saving repo settings…';
    status.className = 'settings-save-status visible';
    return;
  }
  if (dirty) {
    saveButton.disabled = false;
    saveButton.textContent = 'Save repo settings';
    status.textContent = 'Unsaved changes';
    status.className = 'settings-save-status visible';
    return;
  }
  saveButton.disabled = true;
  if (state.repoSettingsSavedAt) {
    saveButton.textContent = 'Saved';
    saveButton.classList.add('is-saved');
    status.textContent = 'Repo promotion settings saved.';
    status.className = 'settings-save-status visible success';
  } else {
    saveButton.textContent = 'Save repo settings';
    status.textContent = 'No changes yet';
    status.className = 'settings-save-status';
  }
}

function renderSettingsPage() {
  if (!settingsPanel || !settingsContent) return;
  settingsPanel.hidden = state.view !== 'settings';
  if (state.view !== 'settings') return;
  const workspace = state.workspace;
  if (!workspace) {
    settingsContent.innerHTML = '<div class="empty">No workspace loaded.</div>';
    return;
  }
  const project = workspace.project || {};
  state.repoSettingsBaseline = repoSettingsSignature({
    promotion_mode: project.promotion_mode || '',
    repo_provider: project.repo_provider || '',
    repo_name: project.repo_name || '',
    base_branch: project.base_branch || 'main',
  });
  settingsContent.innerHTML = `
    <div class="settings-shell">
      <section class="settings-hero">
        <h3>Repository promotion settings</h3>
        <p class="settings-helper">Choose how approved objective code gets applied back to the repo. These settings are project-level, not objective-level.</p>
      </section>
      <section class="settings-card">
        <div class="settings-grid">
          <label>
            Promotion mode
            <select id="settings-repo-promotion-mode">
              <option value="direct_main" ${project.promotion_mode === 'direct_main' ? 'selected' : ''}>Direct to main</option>
              <option value="branch_only" ${project.promotion_mode === 'branch_only' ? 'selected' : ''}>Branch only</option>
              <option value="branch_and_pr" ${project.promotion_mode === 'branch_and_pr' ? 'selected' : ''}>Branch and PR</option>
            </select>
          </label>
          <label>
            Repo provider
            <select id="settings-repo-provider">
              <option value="github" ${project.repo_provider === 'github' ? 'selected' : ''}>GitHub</option>
              <option value="gitlab" ${project.repo_provider === 'gitlab' ? 'selected' : ''}>GitLab</option>
            </select>
          </label>
          <label class="full">
            Repository
            <input id="settings-repo-name" type="text" value="${escapeHtml(project.repo_name || '')}" placeholder="owner/repo" />
          </label>
          <label>
            Base branch
            <input id="settings-repo-base-branch" type="text" value="${escapeHtml(project.base_branch || 'main')}" placeholder="main" />
          </label>
        </div>
        <div class="settings-actions">
          <button id="repo-settings-save-btn" type="button" class="settings-save-button">Save repo settings</button>
          <div id="repo-settings-save-status" class="settings-save-status"></div>
        </div>
      </section>
    </div>
  `;
  settingsContent.querySelectorAll('input, select').forEach((element) => {
    element.addEventListener('input', syncRepoSettingsButtonState);
    element.addEventListener('change', syncRepoSettingsButtonState);
  });
  const saveButton = document.getElementById('repo-settings-save-btn');
  if (saveButton instanceof HTMLButtonElement) {
    saveButton.addEventListener('click', handleSaveRepoPromotionSettings);
  }
  syncRepoSettingsButtonState();
}

function renderObjectives() {
  objectiveList.innerHTML = '';
  const workspace = state.workspace;
  if (atomicObjectiveSelect) {
    atomicObjectiveSelect.innerHTML = '';
  }
  if (!workspace || workspace.objectives.length === 0) {
    objectiveList.innerHTML = '<div class="empty">No objectives yet.</div>';
    objectiveBanner.hidden = true;
    objectiveTitle.textContent = 'No objective selected';
    objectiveSummary.textContent = 'Create an objective to anchor intent and process control.';
    objectiveGate.innerHTML = '<div class="empty">No execution gate data yet.</div>';
    nextActionTitle.textContent = 'No objective selected';
    nextActionBody.textContent = 'Create or select an objective to get a guided next step.';
    expectationRole.textContent = 'You are the Operator';
    expectationNeed.textContent = 'Select or create one objective.';
    expectationWhy.textContent = 'The harness can only guide one active objective at a time.';
    expectationDone.textContent = 'Done when one objective is selected.';
    mermaidMeta.textContent = 'No Mermaid artifact yet.';
    intentSummary.value = '';
    successDefinition.value = '';
    nonNegotiables.value = '';
    frustrationSignals.value = '';
    applyFocusMode(null);
    return;
  }
  if (atomicObjectiveSelect) {
    for (const objective of workspace.objectives) {
      const option = document.createElement('option');
      option.value = objective.id;
      option.textContent = objective.title;
      option.selected = objective.id === state.objectiveId;
      atomicObjectiveSelect.appendChild(option);
    }
  }
  for (const objective of workspace.objectives) {
    const button = document.createElement('button');
    button.className = objective.id === state.objectiveId ? 'active' : '';
    button.innerHTML = `<span class="title">${escapeHtml(objective.title)}</span><span class="meta">${objective.status}</span>`;
    button.addEventListener('click', () => {
      setObjectiveId(objective.id);
      state.taskId = null;
      state.runId = null;
      state.manualFocusMode = null;
      setSidebarCollapsed(true);
      renderObjectives();
      renderTasks();
      renderRuns();
      loadRunOutput();
      renderWorkspaceChrome();
      renderDiagram();
    });
    objectiveList.appendChild(button);
  }
  const selected = currentObjective() || workspace.objectives[0];
  setObjectiveId(selected.id);
  state.manualFocusMode = null;
  objectiveBanner.hidden = false;
  objectiveBannerTitle.textContent = selected.title;
  objectiveBannerMeta.textContent = `${currentFocusMode(selected).replaceAll('_', ' ')} · ${selected.status}`;
  objectiveTitle.textContent = selected.title;
  objectiveSummary.textContent = selected.summary || 'No objective summary recorded.';
  const nextAction = describeNextAction(selected);
  const expectation = expectationForMode(currentFocusMode(selected));
  nextActionTitle.textContent = nextAction.title;
  nextActionBody.textContent = nextAction.body;
  expectationRole.textContent = expectation.role;
  expectationNeed.textContent = expectation.need;
  expectationWhy.textContent = expectation.why;
  expectationDone.textContent = expectation.done;
  renderSavedAnswers(selected);
  renderInterrogationReview(selected);
  renderConversationTranscript(selected);
  proposalActions.hidden = true;
  proposalActions.innerHTML = '';
    if (selected.diagram) {
      renderMermaidMeta(selected);
      if (selected.diagram_proposal) {
        mermaidProposalSummary.hidden = false;
        mermaidProposalSummary.innerHTML = `
        <div class="question">Proposed Mermaid update</div>
        <div class="helper">${escapeHtml(selected.diagram_proposal.summary || 'A proposed Mermaid update is ready for review.')}</div>
      `;
        mermaidControls.innerHTML = `
        <button type="button" data-mermaid-action="accept-proposal">Accept Proposed Flowchart</button>
        <button type="button" data-mermaid-action="rewind-proposal">Rewind hard</button>
      `;
        proposalActions.hidden = false;
        proposalActions.innerHTML = mermaidControls.innerHTML;
      } else {
        mermaidProposalSummary.hidden = true;
        mermaidProposalSummary.innerHTML = '';
        mermaidControls.innerHTML = `
        <button type="button" data-mermaid-action="finished">Matches my flow</button>
        <button type="button" data-mermaid-action="paused">Doesn't match yet</button>
      `;
      proposalActions.hidden = currentFocusMode(selected) !== 'mermaid_review';
      proposalActions.innerHTML = mermaidControls.innerHTML;
    }
  } else {
    renderMermaidMeta(selected);
    mermaidProposalSummary.hidden = true;
    mermaidProposalSummary.innerHTML = '';
    proposalActions.hidden = true;
    proposalActions.innerHTML = '';
  }
  const runReviewHtml = conversationPrimaryActions.hidden
    ? ''
    : `<button type="button" data-mermaid-action="${escapeHtml(conversationPrimaryButton.dataset.action || '')}">${escapeHtml(conversationPrimaryButton.textContent || '')}</button>`;
  if (proposalActions.hidden) {
    proposalActions.innerHTML = '';
  } else if (runReviewHtml) {
    proposalActions.innerHTML += runReviewHtml;
  }
  if (selected.execution_gate?.checks?.length) {
    objectiveGate.innerHTML = selected.execution_gate.checks.map((check) => {
      const badge = check.ok ? 'ok' : 'blocked';
      const detail = check.detail ? `<div class="meta">${escapeHtml(check.detail)}</div>` : '';
      return `<div class="comment"><div class="meta">${badge}</div><div>${escapeHtml(check.label)}</div>${detail}</div>`;
    }).join('');
  } else {
    objectiveGate.innerHTML = '<div class="empty">No execution gate data yet.</div>';
  }
  intentSummary.value = selected.intent_model?.intent_summary || '';
  successDefinition.value = selected.intent_model?.success_definition || '';
  nonNegotiables.value = (selected.intent_model?.non_negotiables || []).join('\\n');
  frustrationSignals.value = (selected.intent_model?.frustration_signals || []).join('\\n');
  renderExecutionPanel();
  renderAtomicUnits();
  renderSupervisorStatus();
  renderPromotionReview();
  applyFocusMode(selected);
}

function describeNextAction(objective) {
  const mode = currentFocusMode(objective);
  if (mode === 'desired_outcome') {
    return {
      title: 'Answer the desired outcome',
      body: 'Describe the result you want from this objective. This should be the only thing you need to answer right now.',
    };
  }
  if (mode === 'success_definition') {
    return {
      title: 'Define how success will be measured',
      body: 'Your desired outcome was saved. Now describe how you will know this objective is actually done.',
    };
  }
  if (mode === 'non_negotiables') {
    return {
      title: 'List the non-negotiables',
      body: 'Your outcome and success definition were saved. Now record the constraints the solution must respect.',
    };
  }
  if (mode === 'interrogation_review') {
    return {
      title: 'Answer the next red-team question',
      body: 'The harness will interrogate the plan in this transcript one question at a time before Mermaid review.',
    };
  }
  if (mode === 'mermaid_review') {
    return {
      title: 'Finish or pause Mermaid review',
      body: 'Your planning answers were saved. Execution stays blocked until the current Mermaid is finished. If the process is unclear, pause it and investigate.',
    };
  }
  if (mode === 'run_start') {
    return {
      title: 'Ready to run the first implementation step',
      body: 'Use the box below as a direct harness CLI. Or start the first implementation step when you are ready.',
    };
  }
  if (mode === 'run_review') {
    return {
      title: 'Review the latest attempt',
      body: 'Ask the harness questions here. If you want evidence, open the latest run output and it will appear directly below the conversation.',
    };
  }
  return {
    title: 'Create or select an objective',
    body: 'Choose one objective to continue.',
  };
}

function expectationForMode(mode) {
  if (mode === 'desired_outcome') {
    return {
      role: 'You are the Answerer',
      need: 'Tell the harness what result you want.',
      why: 'The harness cannot plan safely until the objective is explicit.',
      done: 'Done when your desired outcome is saved.',
    };
  }
  if (mode === 'success_definition') {
    return {
      role: 'You are the Answerer',
      need: 'Define how you will recognize success.',
      why: 'The harness needs a completion test before it can judge progress.',
      done: 'Done when success definition is saved.',
    };
  }
  if (mode === 'non_negotiables') {
    return {
      role: 'You are the Answerer',
      need: 'List the hard constraints the solution must respect.',
      why: 'The harness should not plan past requirements it is supposed to obey.',
      done: 'Done when constraints are saved.',
    };
  }
  if (mode === 'interrogation_review') {
    return {
      role: 'You are the Answerer',
      need: 'Answer the next red-team question in the transcript.',
      why: 'The harness is challenging the plan before Mermaid review.',
      done: 'Done when the harness has no more interrogation questions.',
    };
  }
  if (mode === 'mermaid_review') {
    return {
      role: 'You are the Reviewer',
      need: 'Decide whether the diagram matches your intended flow.',
      why: 'Execution stays blocked until the process logic is accurate enough to govern work.',
      done: 'Done when you click Matches my flow or Doesn\'t match yet.',
    };
  }
  if (mode === 'run_start') {
    return {
      role: 'You are the Decider',
      need: 'Either start the implementation step or redirect the harness in the transcript.',
      why: 'The plan is accepted and the harness is ready to attempt one bounded step.',
      done: 'Done when the run starts or you redirect the work.',
    };
  }
  if (mode === 'run_review') {
    return {
      role: 'You are the Reviewer',
      need: 'Review the latest run or ask the harness to interpret it.',
      why: 'The harness already attempted work and needs your judgment before continuing.',
      done: 'Done when you decide to continue, revise, or investigate.',
    };
  }
  return {
    role: 'You are the Operator',
    need: 'Choose an objective.',
    why: 'The harness needs one active target.',
    done: 'Done when one objective is selected.',
  };
}

function renderTasks() {
  taskList.innerHTML = '';
  const workspace = state.workspace;
  const selectedObjectiveId = state.objectiveId;
  const visibleTasks = (workspace?.tasks || []).filter((task) => {
    if (!selectedObjectiveId) return true;
    return task.objective_id === selectedObjectiveId;
  });
  if (!workspace || visibleTasks.length === 0) {
    taskList.innerHTML = selectedObjectiveId
      ? '<div class="empty">No tasks linked to this objective yet.</div>'
      : '<div class="empty">No tasks yet.</div>';
    return;
  }
  if (!visibleTasks.some((item) => item.id === state.taskId)) {
    state.taskId = visibleTasks[0]?.id || null;
  }
  for (const task of visibleTasks) {
    const button = document.createElement('button');
    button.className = task.id === state.taskId ? 'active' : '';
    button.innerHTML = `<span class="title">${escapeHtml(task.title)}</span><span class="meta">${task.status} · ${task.strategy}</span>`;
    button.addEventListener('click', () => {
      state.taskId = task.id;
      state.runId = ([...task.runs].reverse()[0] || {}).id || null;
      loadRunOutput();
      renderTasks();
      renderRuns();
    });
    taskList.appendChild(button);
  }
}

function renderExecutionPanel() {
  const objective = currentObjective();
  const linkedTasks = (state.workspace?.tasks || []).filter((task) => task.objective_id === objective?.id);
  const task = linkedTasks[0] || null;
  const latestRun = [...(task?.runs || [])].reverse()[0] || null;
  if (!objective || !task) {
    executionTitle.textContent = 'No next step is available yet';
    executionObjective.textContent = 'The harness has not created a bounded implementation step for this objective yet.';
    executionTaskMeta.textContent = '';
    executionRunMeta.textContent = '';
    executionExplanation.textContent = '';
    executionPrimaryButton.hidden = true;
    conversationPrimaryActions.hidden = true;
    if (currentFocusMode(objective) === 'mermaid_review' && !proposalActions.hidden) {
      proposalActions.innerHTML = mermaidControls.innerHTML;
    }
    return;
  }
  executionTitle.textContent = humanTaskTitle(task);
  executionObjective.textContent = task.objective || 'No task objective recorded.';
  executionTaskMeta.textContent = `Task status: ${task.status} · strategy: ${task.strategy}`;
  executionRunMeta.textContent = latestRun
    ? `Latest run: attempt ${latestRun.attempt} · ${latestRun.status}`
    : 'No run has started for this slice yet.';
  executionPrimaryButton.hidden = false;
  if (latestRun) {
    executionExplanation.textContent = 'The harness already attempted this implementation step. Review the latest result before deciding whether to continue, revise the plan, or investigate.';
    executionPrimaryButton.textContent = 'Show latest run review';
    executionPrimaryButton.dataset.action = 'review-run';
    conversationPrimaryActions.hidden = false;
    conversationPrimaryButton.textContent = 'Show latest run review';
    conversationPrimaryButton.dataset.action = 'review-run';
  } else {
    executionExplanation.textContent = 'If you start this now, the harness will plan and attempt this one implementation step, write artifacts, and bring you back to a human-readable review of what happened.';
    executionPrimaryButton.textContent = 'Start this implementation step';
    executionPrimaryButton.dataset.action = 'start-run';
    conversationPrimaryActions.hidden = false;
    conversationPrimaryButton.textContent = 'Start this implementation step';
    conversationPrimaryButton.dataset.action = 'start-run';
  }
  if (currentFocusMode(objective) === 'mermaid_review' && !proposalActions.hidden) {
    const runReviewHtml = `<button type="button" data-mermaid-action="${escapeHtml(conversationPrimaryButton.dataset.action || '')}">${escapeHtml(conversationPrimaryButton.textContent || '')}</button>`;
    proposalActions.innerHTML = `${mermaidControls.innerHTML}${runReviewHtml}`;
  }
}

function renderAtomicUnits() {
  if (!atomicPanel || !atomicList || !atomicTitle || !atomicSummary || !atomicGenerationStatus || !atomicGenerationMeta) return;
  const objective = currentObjective();
  if (!objective) {
    atomicTitle.textContent = 'Atomic units of work';
    atomicSummary.textContent = 'Select an objective to inspect the current atomic units.';
    atomicGenerationStatus.textContent = '';
    atomicGenerationMeta.innerHTML = '';
    atomicList.innerHTML = '<div class="empty">No objective selected.</div>';
    return;
  }
  const generation = objective.atomic_generation || { status: 'idle', unit_count: 0 };
  const review = objective.promotion_review || { waived_failed_count: 0, unresolved_failed_count: 0 };
  const linkedTasks = Array.isArray(objective.atomic_units) ? objective.atomic_units : [];
  const publishedCount = linkedTasks.filter((task) => task.published_unit !== false).length;
  const extraTaskCount = Math.max(0, linkedTasks.length - publishedCount);
  atomicTitle.textContent = 'Atomic units of work';
  atomicSummary.textContent = linkedTasks.length
    ? (extraTaskCount > 0
        ? 'Showing live objective tasks. Published Mermaid units appear first, followed by follow-on work created during execution.'
        : 'These atomic units were derived from the accepted flowchart for this objective. Use the CLI to clarify or challenge the decomposition.')
    : 'Atomic units will appear here as the harness derives them from the accepted flowchart.';
  if (generation.status === 'running') {
    const dots = '.'.repeat((Math.floor(Date.now() / 500) % 3) + 1);
    const roundInfo = generation.refinement_round ? ` · Round ${generation.refinement_round}` : '';
    const taskCount = linkedTasks.length ? ` · ${linkedTasks.length} task(s)` : '';
    const critiqueInfo = generation.critique_accepted === true ? ' · Critique: passed' :
      generation.critique_accepted === false ? ' · Critique: needs work' : '';
    const coverageInfo = generation.coverage_complete === true ? ' · Coverage: complete' :
      generation.coverage_complete === false ? ' · Coverage: gaps found' : '';
    atomicGenerationStatus.textContent = `Generating atomic units from Mermaid v${generation.diagram_version}${dots}${roundInfo}${taskCount}${critiqueInfo}${coverageInfo}`;
  } else if (generation.status === 'completed') {
    const tasksDone = linkedTasks.filter((t) => t.status === 'completed').length;
    const tasksFailed = linkedTasks.filter((t) => t.status === 'failed').length;
    const tasksActive = linkedTasks.filter((t) => t.status === 'active').length;
    const tasksPending = linkedTasks.filter((t) => t.status === 'pending').length;
    const allDone = tasksPending === 0 && tasksActive === 0;
    if (allDone && linkedTasks.length > 0) {
      if (review.unresolved_failed_count === 0 && review.waived_failed_count > 0) {
        atomicGenerationStatus.textContent = `Execution finished. ${tasksDone} completed, ${review.waived_failed_count} historical failure(s) waived during review.`;
      } else {
        atomicGenerationStatus.textContent = `All ${linkedTasks.length} task(s) finished. ${tasksDone} completed, ${tasksFailed} failed.`;
      }
    } else if (linkedTasks.length > 0) {
      const decompositionSummary = extraTaskCount > 0
        ? `${publishedCount} tasks split from Mermaid v${generation.diagram_version}, plus ${extraTaskCount} follow-on task(s).`
        : `${linkedTasks.length} atomic tasks split from Mermaid v${generation.diagram_version}.`;
      atomicGenerationStatus.textContent = `${decompositionSummary} ${tasksDone} done, ${tasksActive} active, ${tasksPending} pending, ${tasksFailed} failed.`;
    } else {
      atomicGenerationStatus.textContent = `Decomposition finished but produced no tasks.`;
    }
  } else if (generation.status === 'failed') {
    atomicGenerationStatus.textContent = generation.error || 'Atomic generation failed.';
  } else {
    atomicGenerationStatus.textContent = 'No atomic generation is currently running.';
  }
  const lastActivity = generation.last_activity_at ? formatRelativeTime(generation.last_activity_at) : '';
  const phase = generation.phase || '';
  const pills = [];
  // Determine the effective status: if generation is done but tasks are in flight, show execution status
  const hasInFlightTasks = linkedTasks.some((t) => t.status === 'pending' || t.status === 'active');
  let effectiveStatus;
  let effectiveLabel;
  if (generation.status === 'completed' && hasInFlightTasks) {
    effectiveStatus = 'status-running';
    effectiveLabel = 'Executing';
  } else if (generation.status === 'completed' && linkedTasks.length > 0) {
    if (review.unresolved_failed_count === 0 && review.waived_failed_count > 0) {
      effectiveStatus = 'status-complete';
      effectiveLabel = 'Resolved after review';
    } else {
      const allPassed = linkedTasks.every((t) => t.status === 'completed');
      effectiveStatus = allPassed ? 'status-complete' : 'status-failed';
      effectiveLabel = allPassed ? 'All tasks done' : 'Tasks finished with failures';
    }
  } else if (generation.status === 'running') {
    effectiveStatus = 'status-running';
    effectiveLabel = phase ? `Splitting: ${phase}` : 'Splitting into tasks';
  } else if (generation.status === 'failed') {
    effectiveStatus = 'status-failed';
    effectiveLabel = 'Decomposition failed';
  } else {
    effectiveStatus = 'status-idle';
    effectiveLabel = 'Idle';
  }
  const liveClass = effectiveStatus === 'status-running' ? ' live' : '';
  pills.push(`<span class="pill ${effectiveStatus}${liveClass}">${escapeHtml(effectiveLabel)}</span>`);
  if (generation.status === 'running' && generation.refinement_round) {
    pills.push(`<span class="pill">Round ${generation.refinement_round}</span>`);
  }
  if (linkedTasks.length) {
    const taskLabel = extraTaskCount > 0 ? 'live task(s)' : 'task(s)';
    pills.push(`<span class="pill">${linkedTasks.length} ${taskLabel}</span>`);
  }
  if (generation.status === 'running' && generation.critique_accepted === true) {
    pills.push(`<span class="pill status-complete">Critique: passed</span>`);
  } else if (generation.status === 'running' && generation.critique_accepted === false) {
    pills.push(`<span class="pill status-running">Critique: needs work</span>`);
  }
  if (generation.status === 'running' && generation.coverage_complete === true) {
    pills.push(`<span class="pill status-complete">Coverage: complete</span>`);
  } else if (generation.status === 'running' && generation.coverage_complete === false) {
    pills.push(`<span class="pill status-running">Coverage: gaps found</span>`);
  }
  if (review.waived_failed_count > 0) {
    pills.push(`<span class="pill status-complete">${review.waived_failed_count} waived failure(s)</span>`);
  }
  if (review.unresolved_failed_count > 0) {
    pills.push(`<span class="pill status-failed">${review.unresolved_failed_count} blocking failure(s)</span>`);
  }
  if (generation.last_activity_at) {
    const roundTag = generation.refinement_round ? ` (round ${generation.refinement_round})` : '';
    pills.push(`<span class="pill last-activity-pill" data-timestamp="${generation.last_activity_at}">Last activity ${escapeHtml(lastActivity)}${roundTag}</span>`);
  }
  atomicGenerationMeta.innerHTML = pills.join('');
  if (!linkedTasks.length) {
    atomicList.innerHTML = '<div class="empty">No atomic units yet.</div>';
    return;
  }
  // Counts
  const counts = { all: linkedTasks.length, completed: 0, working: 0, validating: 0, active: 0, failed: 0, pending: 0 };
  for (const task of linkedTasks) {
    const s = task.status || 'pending';
    if (s === 'working') counts.working++;
    else if (s === 'validating') counts.validating++;
    else if (s === 'active') counts.active++;
    else if (s in counts) counts[s]++;
    else counts.pending++;
  }
  // "Active" tab combines working + validating + active
  const totalActive = counts.working + counts.validating + counts.active;
  const total = linkedTasks.length;
  const pct = (n) => total > 0 ? ((n / total) * 100).toFixed(1) + '%' : '0%';
  // Auto-select a tab that has content if current tab is empty
  const activeStatuses = new Set(['active', 'working', 'validating']);
  const tabCounts = { all: counts.all, active: totalActive, pending: counts.pending, completed: counts.completed, failed: counts.failed };
  if (state.atomicTab !== 'all' && (tabCounts[state.atomicTab] || 0) === 0) {
    state.atomicTab = 'all';
  }
  const filteredTasks = state.atomicTab === 'all'
    ? linkedTasks
    : state.atomicTab === 'active'
      ? linkedTasks.filter((t) => activeStatuses.has(t.status || 'pending'))
      : linkedTasks.filter((t) => (t.status || 'pending') === state.atomicTab);
  // Tabs + progress bar
  const tabs = ['all', 'active', 'pending', 'completed', 'failed'];
  const tabLabels = { all: 'All', active: 'Active', pending: 'Pending', completed: 'Done', failed: 'Failed' };
  const tabsHtml = `
    <div class="atomic-status-tabs" id="atomic-tabs">
      ${tabs.map((tab) => `<button class="${tab === state.atomicTab ? 'active' : ''}" data-tab="${tab}">${tabLabels[tab]}<span class="tab-count">${tabCounts[tab] || 0}</span></button>`).join('')}
      ${counts.failed > 0 ? `<button class="retry-all-btn" id="retry-all-failed">Retry all failed</button>` : ''}
    </div>
  `;
  const progressBar = `
    <div class="atomic-progress-bar">
      <div class="segment completed" style="width:${pct(counts.completed)}"></div>
      <div class="segment active" style="width:${pct(counts.working)}; background:#4a90d9"></div>
      <div class="segment active" style="width:${pct(counts.validating)}; background:#2f6f4f"></div>
      <div class="segment failed" style="width:${pct(counts.failed)}"></div>
      <div class="segment pending" style="width:${pct(counts.pending)}"></div>
    </div>
  `;
  const cardsHtml = filteredTasks.length
    ? filteredTasks.map((task) => {
        const latestRun = task.latest_run || null;
        const status = task.status || 'pending';
        const displayStatus = latestRun && activeStatuses.has(status) ? latestRun.status : status;
        const attemptText = latestRun ? `#${latestRun.attempt}` : '';
        const isExpanded = task.id === state.taskId;
        const runtimeText = (() => {
          if (!latestRun || !latestRun.started_at) return '';
          if (!latestRun.finished_at) return ''; // Active timers are updated by the 1s tick
          const start = new Date(latestRun.started_at);
          const end = new Date(latestRun.finished_at);
          const secs = Math.max(0, Math.floor((end - start) / 1000));
          if (secs < 60) return secs + 's';
          const mins = Math.floor(secs / 60);
          const rem = secs % 60;
          if (mins < 60) return mins + 'm ' + rem + 's';
          const hrs = Math.floor(mins / 60);
          return hrs + 'h ' + (mins % 60) + 'm';
        })();
        const isActiveTimer = latestRun && latestRun.started_at && !latestRun.finished_at;
        const valSummary = (() => {
          const v = latestRun?.validation;
          if (!v) return '';
          const compile = v.compile_passed ? '<span class="pass">compile: pass</span>' : '<span class="fail">compile: fail</span>';
          const tests = v.test_passed ? '<span class="pass">tests: pass</span>' : (v.test_timed_out ? '<span class="fail">tests: timeout</span>' : '<span class="fail">tests: fail</span>');
          return `${compile} · ${tests}`;
        })();
        return `
          <div class="atomic-card ${isExpanded ? 'active expanded' : ''}" data-atomic-task="${task.id}">
            <div class="status-bar ${displayStatus}"></div>
            <div class="card-content">
              <div class="card-header">
                <div class="title">${escapeHtml(task.title)}</div>
                <span class="status-pill ${displayStatus}">${escapeHtml(displayStatus)}</span>
                ${attemptText ? `<span class="attempt-count">${attemptText}</span>` : ''}
                ${isActiveTimer ? `<span class="runtime active-timer" data-started="${latestRun.started_at}"></span>` : ''}
                ${runtimeText ? `<span class="runtime">${runtimeText}</span>` : ''}
              </div>
              ${valSummary ? `<div class="validation-summary inline">${valSummary}</div>` : ''}
              <div class="meta">${escapeHtml(task.objective || '').split('\\n')[0]}</div>
              <div class="body">${escapeHtml(task.objective || 'No task objective recorded.')}${task.rationale ? `\n\nWhy this unit exists: ${escapeHtml(task.rationale)}` : ''}</div>
              ${status === 'failed' ? `<button class="retry-btn" data-retry-task="${task.id}">Retry</button>` : ''}
            </div>
          </div>
        `;
      }).join('')
    : `<div class="empty">No ${state.atomicTab} tasks.</div>`;
  const newHtml = tabsHtml + progressBar + cardsHtml;
  // Skip DOM rebuild if content is unchanged (avoids scroll/selection disruption).
  if (atomicList._lastHtml === newHtml) return;
  atomicList._lastHtml = newHtml;
  const scrollTop = atomicList.scrollTop;
  atomicList.innerHTML = newHtml;
  atomicList.scrollTop = scrollTop;
  // Wire tab clicks
  const tabContainer = document.getElementById('atomic-tabs');
  if (tabContainer) {
    tabContainer.addEventListener('click', (event) => {
      const tab = event.target.closest('[data-tab]');
      if (!tab) return;
      state.atomicTab = tab.dataset.tab;
      atomicList._lastHtml = null; // force rebuild on tab change
      renderAtomicUnits();
    });
  }
}

function renderSupervisorStatus() {
  const panel = document.getElementById('supervisor-panel');
  if (!panel) return;
  const supervisor = state.workspace?.supervisor || {};
  const isRunning = supervisor.running || supervisor.state === 'running' || supervisor.state === 'starting' || supervisor.state === 'stopping';
  const isStopping = supervisor.state === 'stopping';
  const hasHistory = supervisor.state === 'finished' || supervisor.state === 'error';
  const startBtn = document.getElementById('supervisor-start-btn');
  const stopBtn = document.getElementById('supervisor-stop-btn');
  const statusEl = document.getElementById('supervisor-status');
  const metaEl = document.getElementById('supervisor-meta');
  // Scope counts to current objective
  const objective = currentObjective();
  const objTasks = Array.isArray(objective?.atomic_units) ? objective.atomic_units : [];
  const review = objective?.promotion_review || {};
  const objCounts = { completed: 0, active: 0, failed: 0, pending: 0 };
  for (const t of objTasks) {
    const s = t.status || 'pending';
    if (s in objCounts) objCounts[s]++;
  }
  const objTotal = objTasks.length;
  // Only show the panel when harness is active or has something to report
  panel.hidden = !isRunning && !hasHistory;
  // Start button only when stopped after having run (restart), never on initial idle
  if (startBtn) startBtn.hidden = isRunning || !hasHistory;
  if (stopBtn) {
    stopBtn.hidden = !isRunning;
    if (isStopping) {
      stopBtn.textContent = 'Stopping...';
      stopBtn.disabled = true;
    } else {
      stopBtn.textContent = 'Stop harness';
      stopBtn.disabled = false;
    }
  }
  if (statusEl) {
    if (objective?.status === 'resolved' && objCounts.active === 0 && objCounts.pending === 0) {
      statusEl.textContent = 'Execution is finished for this objective. Promotion review is the next step.';
    } else if (supervisor.state === 'stopping') {
      statusEl.textContent = 'Stopping harness... waiting for current worker to finish.';
    } else if (supervisor.state === 'running' || supervisor.state === 'starting') {
      const dots = '.'.repeat((Math.floor(Date.now() / 500) % 3) + 1);
      statusEl.textContent = objTotal
        ? `The harness is working through your tasks${dots} ${objCounts.completed}/${objTotal} done.`
        : `The harness is working through your tasks${dots}`;
    } else if (supervisor.state === 'finished') {
      const reason = supervisor.exit_reason === 'idle' ? 'No more tasks to process.'
        : supervisor.exit_reason === 'graceful_stop_requested' ? 'Stopped by operator.'
        : supervisor.exit_reason === 'max_iterations_reached' ? 'Reached iteration limit.'
        : 'Finished.';
      statusEl.textContent = objTotal
        ? `${reason} ${objCounts.completed}/${objTotal} tasks completed.`
        : `${reason}`;
    } else if (supervisor.state === 'error') {
      statusEl.textContent = `Something went wrong while processing tasks. You can restart to retry.`;
    } else {
      statusEl.textContent = '';
    }
  }
  if (metaEl) {
    const pills = [];
    if (objective?.status === 'resolved' && objCounts.active === 0 && objCounts.pending === 0) {
      pills.push('<span class="pill status-complete">Objective resolved</span>');
      if ((review.unresolved_failed_count || 0) > 0) {
        pills.push(`<span class="pill status-failed">${review.unresolved_failed_count} blocker(s)</span>`);
      } else if ((review.waived_failed_count || 0) > 0) {
        pills.push(`<span class="pill status-complete">${review.waived_failed_count} waived failure(s)</span>`);
      }
      if (review.ready) {
        pills.push('<span class="pill status-complete">Promotion review ready</span>');
      }
    } else {
      const stateClass = isRunning ? 'status-running' : supervisor.state === 'finished' ? 'status-complete' : supervisor.state === 'error' ? 'status-failed' : 'status-idle';
      const liveClass = isRunning ? ' live' : '';
      pills.push(`<span class="pill ${stateClass}${liveClass}">${escapeHtml(supervisor.state || 'idle')}</span>`);
    }
    if (objTotal) {
      pills.push(`<span class="pill">${objCounts.completed}/${objTotal} done</span>`);
    }
    if (supervisor.last_task_title && isRunning) {
      const friendlyTitle = supervisor.last_task_title.replace(/: repair executor\/runtime failure$/i, '').replace(/: repair .*$/i, '');
      pills.push(`<span class="pill">Working on: ${escapeHtml(friendlyTitle)}</span>`);
    }
    if (supervisor.last_event_at) {
      pills.push(`<span class="pill last-activity-pill" data-timestamp="${escapeHtml(supervisor.last_event_at)}">Last activity ${escapeHtml(formatRelativeTime(supervisor.last_event_at))}</span>`);
    }
    metaEl.innerHTML = pills.join('');
  }
}

function renderPromotionReview() {
  if (!promotionReviewPanel || !promotionReviewTitle || !promotionReviewSummary || !promotionReviewMeta || !promotionReviewContent || !promotionReviewRoundsPanel || !promotionReviewRoundsContent) return;
  const objective = currentObjective();
  if (!objective) {
    promotionReviewPanel.hidden = state.view !== 'promotion-review';
    promotionReviewRoundsPanel.hidden = state.view !== 'promotion-review';
    promotionReviewTitle.textContent = 'Promotion review';
    promotionReviewSummary.textContent = 'Select an objective to inspect promotion readiness and recorded reviews.';
    promotionReviewMeta.innerHTML = '';
    promotionReviewContent.innerHTML = '<div class="empty">No objective selected.</div>';
    promotionReviewRoundsContent.innerHTML = '';
    return;
  }
  const review = objective.promotion_review || {};
  const repoPromotion = objective.repo_promotion || {};
  const projectSettings = repoPromotion.project_settings || {};
  const candidate = repoPromotion.candidate || null;
  const latestRepoPromotion = repoPromotion.latest_promotion || null;
  const applyback = latestRepoPromotion?.applyback || {};
  const counts = review.task_counts || {};
  const rounds = Array.isArray(review.review_rounds) ? review.review_rounds : [];
  const latestRound = rounds[0] || null;
  const latestGradedRound = rounds.find((round) => (round.packet_count || 0) > 0) || null;
  const packets = Array.isArray(review.review_packets) ? review.review_packets : [];
  const failedTasks = Array.isArray(review.failed_tasks) ? review.failed_tasks : [];
  const reviewState = review.objective_review_state || {};
  const operatorOverride = review.operator_override || null;
  const verdictCounts = (latestRound && latestRound.verdict_counts) || review.verdict_counts || {};
  function humanizeDimension(value) {
    const raw = String(value || '').trim();
    return raw ? raw.replaceAll('_', ' ') : 'dimension review';
  }
  function humanizeReviewer(value, dimension) {
    const raw = String(value || '').trim().toLowerCase();
    const aliases = {
      arch: 'Architecture reviewer',
      ops: 'DevOps reviewer',
      arbiter: 'Atomicity reviewer',
      qa: 'QA reviewer',
      security: 'Security reviewer',
    };
    if (aliases[raw]) return aliases[raw];
    if (raw.endsWith(' agent')) return raw[0].toUpperCase() + raw.slice(1);
    if (raw) return raw.split(/\s+/).map((part) => part ? part[0].toUpperCase() + part.slice(1) : '').join(' ');
    return `${humanizeDimension(dimension)} reviewer`;
  }
  function verdictLabel(value) {
    const raw = String(value || '').trim();
    if (!raw) return 'Unknown';
    if (raw === 'remediation_required') return 'Remediation';
    return raw.replaceAll('_', ' ');
  }
  function progressLabel(value) {
    const raw = String(value || '').trim();
    if (!raw || raw === 'not_applicable') return '';
    if (raw === 'still_blocking') return 'Still blocking';
    if (raw === 'new_concern') return 'New concern';
    return raw.replaceAll('_', ' ');
  }
  function aggregateUsage(packetList) {
    const usage = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0, latency_ms: 0 };
    for (const packet of Array.isArray(packetList) ? packetList : []) {
      const llmUsage = packet?.llm_usage && typeof packet.llm_usage === 'object' ? packet.llm_usage : {};
      usage.prompt_tokens += Number(llmUsage.prompt_tokens || 0);
      usage.completion_tokens += Number(llmUsage.completion_tokens || 0);
      usage.total_tokens += Number(llmUsage.total_tokens || 0);
      usage.cost_usd += Number(llmUsage.cost_usd || 0);
      usage.latency_ms += Number(llmUsage.latency_ms || 0);
    }
    return usage;
  }
  function usageBits(usage) {
    const bits = [];
    if ((usage.total_tokens || 0) > 0) bits.push(`${usage.total_tokens} tokens`);
    if ((usage.cost_usd || 0) > 0) bits.push(`$${Number(usage.cost_usd).toFixed(4)}`);
    if ((usage.latency_ms || 0) > 0) bits.push(`${Math.round(Number(usage.latency_ms))}ms`);
    return bits;
  }
  promotionReviewPanel.hidden = state.view !== 'promotion-review';
  promotionReviewRoundsPanel.hidden = state.view !== 'promotion-review';
  promotionReviewTitle.textContent = `Promotion review for ${objective.title}`;
  const reviewHasStarted = rounds.length > 0;
  if (reviewState.status === 'running') {
    promotionReviewSummary.textContent = `Automatic objective promotion review round ${latestRound?.round_number || ''} is running. Reviewer packets will appear here as each agent finishes.`;
  } else if (operatorOverride) {
    promotionReviewSummary.textContent = review.next_action || 'An operator override approved the latest objective review round.';
  } else if (!reviewHasStarted) {
    promotionReviewSummary.textContent = 'Promotion review has not started yet. No agent review packets are recorded yet. Use the harness output panel on the left to ask questions while the harness prepares review work.';
  } else {
    promotionReviewSummary.textContent = review.next_action || 'Review promotion packets, blockers, and waivers before promoting this objective.';
  }
  const pills = [];
  if (latestRound) {
    const roundStateLabel =
      latestRound.operator_override ? 'Operator approved' :
      latestRound.status === 'remediating' ? 'Back in Atomic' :
      latestRound.status === 'ready_for_rerun' ? 'Ready for re-review' :
      latestRound.status === 'running' ? 'Review running' :
      latestRound.status === 'passed' ? 'Round passed' :
      latestRound.status === 'failed' ? 'Review failed' :
      latestRound.status === 'needs_remediation' ? 'Remediation needed' :
      'Review active';
    pills.push(`<span class="pill ${latestRound.operator_override || latestRound.status === 'passed' ? 'status-complete' : latestRound.status === 'failed' || latestRound.status === 'needs_remediation' ? 'status-failed' : 'status-running'}">${escapeHtml(roundStateLabel)}</span>`);
    const reviewerPills = (Array.isArray(latestRound.packets) ? latestRound.packets : []).map((packet) => {
      const verdict = String(packet.verdict || '').trim();
      const reviewer = humanizeReviewer(packet.reviewer, packet.dimension);
      const klass = verdict === 'pass' ? 'status-complete' : verdict === 'remediation_required' ? 'status-failed' : 'status-running';
      return `<span class="pill promotion-verdict-pill ${klass}">${escapeHtml(`${reviewer}: ${verdictLabel(verdict)}`)}</span>`;
    });
    promotionReviewMeta.innerHTML = `
      <div class="promotion-reviewer-strip">
        ${pills.join('')}
        ${reviewerPills.join('')}
      </div>
    `;
  } else {
    if (reviewState.status === 'running') {
      pills.push('<span class="pill status-running live">Automatic review running</span>');
    } else if (review.phase === 'promotion_review_pending' && review.ready) {
      pills.push('<span class="pill status-running">Ready for next review round</span>');
    } else if (review.review_clear) {
      pills.push('<span class="pill status-complete">Promotion review passed</span>');
    } else {
      pills.push(`<span class="pill ${review.ready ? 'status-complete' : 'status-running'}">${review.ready ? 'Packets generated' : 'Needs review'}</span>`);
    }
    promotionReviewMeta.innerHTML = pills.join('');
  }
  if (latestGradedRound && !state.selectedPromotionReportKey) {
    const firstPacket = Array.isArray(latestGradedRound.packets) ? latestGradedRound.packets[0] : null;
    if (firstPacket) {
      state.selectedPromotionReportKey = `${latestGradedRound.review_id || latestGradedRound.round_number}:${firstPacket.dimension || humanizeReviewer(firstPacket.reviewer, firstPacket.dimension)}`;
    }
  }
  const latestUsage = aggregateUsage(Array.isArray(latestRound?.packets) ? latestRound.packets : []);
  const forcePromotionAction = review.can_force_promote ? `
    <div class="promotion-summary-actions">
      <button id="promotion-force-approve-btn" class="secondary-button" type="button">Force Promote</button>
    </div>
  ` : '';
  const summaryCards = `
    <div class="promotion-summary-grid">
      <div class="promotion-summary-card"><div class="label">Review rounds</div><div class="value">${rounds.length || 0}</div></div>
      <div class="promotion-summary-card"><div class="label">Review packets</div><div class="value">${review.objective_review_packet_count || 0}</div></div>
      <div class="promotion-summary-card"><div class="label">Concerns</div><div class="value">${verdictCounts.concern || 0}</div></div>
      <div class="promotion-summary-card"><div class="label">Waived failures</div><div class="value">${review.waived_failed_count || 0}</div></div>
      <div class="promotion-summary-card"><div class="label">Latest round usage</div><div class="value" style="font-size:1rem">${escapeHtml(usageBits(latestUsage).join(' · ') || 'No LLM usage yet')}</div></div>
    </div>
    ${forcePromotionAction}
    ${operatorOverride ? `<div class="hint">Operator override by ${escapeHtml(operatorOverride.author || 'operator')} at ${escapeHtml(formatRelativeTime(operatorOverride.created_at))}: ${escapeHtml(operatorOverride.rationale || 'No rationale recorded.')}</div>` : ''}
  `;
  const renderPacket = (packet) => {
    const latest = packet.latest || {};
    const isObjectivePacket = packet.source === 'objective_review';
    const details = latest.details || {};
    const validators = Array.isArray(details.validators) ? details.validators : [];
    const affirmation = details.affirmation || {};
    const issues = isObjectivePacket
      ? (Array.isArray(packet.findings) ? packet.findings.map((finding) => ({ summary: finding })) : [])
      : validators.flatMap((validator) => Array.isArray(validator.issues) ? validator.issues : []);
    const status = isObjectivePacket ? (packet.verdict || 'unknown') : (latest.status || 'unknown');
    const rationale = isObjectivePacket ? (packet.summary || 'No review rationale recorded.') : (affirmation.rationale || latest.summary || 'No review rationale recorded.');
    const title = isObjectivePacket
      ? humanizeDimension(packet.dimension || 'dimension review')
      : (packet.task_title || packet.task_id || 'Promotion review');
    const reviewerLabel = isObjectivePacket ? humanizeReviewer(packet.reviewer, packet.dimension) : '';
    const subStatus = isObjectivePacket ? '' : `Task status: ${packet.task_status || 'unknown'}`;
    const recordedAt = isObjectivePacket ? packet.created_at : latest.created_at;
    const evidence = Array.isArray(packet.evidence) ? packet.evidence : [];
    const backend = packet.backend || affirmation.backend || '';
    const progressStatus = String(packet.progress_status || '').trim();
    const closureCriteria = String(packet.closure_criteria || '').trim();
    const evidenceRequired = String(packet.evidence_required || '').trim();
    const repeatReason = String(packet.repeat_reason || '').trim();
    const ownerScope = String(packet.owner_scope || '').trim();
    const severity = String(packet.severity || '').trim();
    const llmUsage = packet.llm_usage && typeof packet.llm_usage === 'object' ? packet.llm_usage : {};
    const llmUsageBits = usageBits(llmUsage);
    const usageReported = packet.llm_usage_reported !== false;
    return `
      <article class="promotion-packet">
        <div class="promotion-packet-title">${escapeHtml(title)}</div>
        <div class="promotion-packet-meta">
          <span class="pill promotion-verdict-pill ${status === 'approved' || status === 'pass' ? 'status-complete' : status === 'rejected' || status === 'remediation_required' ? 'status-failed' : 'status-running'}">${escapeHtml(verdictLabel(status))}</span>
          ${reviewerLabel ? `<span class="pill promotion-opinion-pill">${escapeHtml(reviewerLabel)}</span>` : ''}
          ${severity ? `<span class="pill promotion-opinion-pill">${escapeHtml(`Severity: ${severity}`)}</span>` : ''}
          ${ownerScope ? `<span class="pill promotion-opinion-pill">${escapeHtml(`Scope: ${ownerScope}`)}</span>` : ''}
          ${progressLabel(progressStatus) ? `<span class="pill promotion-opinion-pill ${progressStatus === 'resolved' ? 'status-complete' : progressStatus === 'improving' ? 'status-running' : progressStatus === 'still_blocking' || progressStatus === 'new_concern' ? 'status-failed' : ''}">${escapeHtml(progressLabel(progressStatus))}</span>` : ''}
          ${subStatus ? `<span class="pill">${escapeHtml(subStatus)}</span>` : ''}
          ${recordedAt ? `<span class="pill">Recorded ${escapeHtml(formatRelativeTime(recordedAt))}</span>` : ''}
          ${llmUsageBits.length ? `<span class="pill">${escapeHtml(llmUsageBits.join(' · '))}</span>` : (!usageReported ? `<span class="pill status-idle">Usage not reported</span>` : '')}
        </div>
        <div class="promotion-packet-summary">${escapeHtml(rationale)}</div>
        ${backend ? `<div class="hint">Model: ${escapeHtml(backend)}</div>` : ''}
        ${(closureCriteria || evidenceRequired)
          ? `<div class="promotion-requirements">
              ${closureCriteria ? `<div class="promotion-requirement-block"><div class="label">Closure criteria</div><div class="promotion-requirement-body">${escapeHtml(closureCriteria)}</div></div>` : ''}
              ${evidenceRequired ? `<div class="promotion-requirement-block"><div class="label">Evidence required</div><div class="promotion-requirement-body">${escapeHtml(evidenceRequired)}</div></div>` : ''}
              ${repeatReason ? `<div class="promotion-requirement-block"><div class="label">Why still open</div><div class="promotion-requirement-body">${escapeHtml(repeatReason)}</div></div>` : ''}
            </div>`
          : ''
        }
        ${issues.length ? `<ul class="promotion-packet-issues">${issues.map((issue) => `<li>${escapeHtml(issue.summary || issue.code || 'Review issue')}</li>`).join('')}</ul>` : '<div class="hint">No validator issues recorded on the latest packet.</div>'}
        ${evidence.length ? `<ul class="promotion-packet-evidence">${evidence.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : ''}
      </article>
    `;
  };
  const roundHtml = rounds.length ? rounds.map((round) => {
    const roundVerdicts = round.verdict_counts || {};
    const remediationCounts = round.remediation_counts || {};
    const roundPills = [];
    if (round.status === 'passed') {
      roundPills.push(`<span class="pill status-complete">${round.operator_override ? 'Operator approved' : 'Passed'}</span>`);
    } else if (round.status === 'running') {
      roundPills.push('<span class="pill status-running live">Running</span>');
    } else if (round.status === 'remediating') {
      roundPills.push('<span class="pill status-running">Back in Atomic</span>');
    } else if (round.status === 'ready_for_rerun') {
      roundPills.push('<span class="pill status-running">Ready for re-review</span>');
    } else if (round.status === 'needs_remediation') {
      roundPills.push('<span class="pill status-failed">Remediation needed</span>');
    } else if (round.status === 'failed') {
      roundPills.push('<span class="pill status-failed">Review failed</span>');
    }
    if ((roundVerdicts.pass || 0) > 0) roundPills.push(`<span class="pill status-complete">${roundVerdicts.pass} pass</span>`);
    if ((roundVerdicts.concern || 0) > 0) roundPills.push(`<span class="pill status-running">${roundVerdicts.concern} concern</span>`);
    if ((roundVerdicts.remediation_required || 0) > 0) roundPills.push(`<span class="pill status-failed">${roundVerdicts.remediation_required} remediation</span>`);
    if ((remediationCounts.total || 0) > 0) {
      roundPills.push(`<span class="pill">${remediationCounts.total} remediation task(s)</span>`);
    }
    const openRemediationCount = (remediationCounts.active || 0) + (remediationCounts.pending || 0);
    const packetList = Array.isArray(round.packets) ? round.packets : [];
    const remediationList = Array.isArray(round.remediation_tasks) ? round.remediation_tasks : [];
    const roundUsage = aggregateUsage(packetList);
    const summaryStats = [
      { label: 'Round status', value: round.status === 'remediating' ? 'Back in Atomic' : round.status === 'ready_for_rerun' ? 'Ready for re-review' : round.status === 'needs_remediation' ? 'Remediation needed' : round.status === 'running' ? 'Review running' : round.status === 'passed' ? (round.operator_override ? 'Operator approved' : 'Passed') : round.status === 'failed' ? 'Review failed' : round.status },
      { label: 'Pass', value: String(roundVerdicts.pass || 0), tone: 'status-complete' },
      { label: 'Concern', value: String(roundVerdicts.concern || 0), tone: (roundVerdicts.concern || 0) > 0 ? 'status-running' : '' },
      { label: 'Remediation tasks', value: String(remediationCounts.total || 0) },
      { label: 'Open', value: String(openRemediationCount), tone: openRemediationCount > 0 ? 'status-running' : '' },
      { label: 'LLM usage', value: usageBits(roundUsage).join(' · ') || 'Not recorded' },
    ];
    if ((remediationCounts.completed || 0) > 0 || (remediationCounts.total || 0) > 0) {
      summaryStats.push({ label: 'Done', value: String(remediationCounts.completed || 0), tone: (remediationCounts.completed || 0) > 0 ? 'status-complete' : '' });
    }
    const remediationHtml = remediationList.length ? `
      <div class="promotion-round-remediation">
        <div class="label">Remediation tasks</div>
        <ul class="promotion-packet-issues">
          ${remediationList.map((task) => `<li>${escapeHtml(task.title || task.id || 'Task')} <span class="hint">(${escapeHtml(task.status || 'unknown')})</span></li>`).join('')}
        </ul>
      </div>
    ` : '';
    return `
      <section class="promotion-round">
        <div class="promotion-section-title">
          <h4>Round ${escapeHtml(String(round.round_number || '?'))}</h4>
        <span class="pill last-activity-pill" data-timestamp="${escapeHtml(round.last_activity_at || '')}">${round.last_activity_at ? `Last activity ${escapeHtml(formatRelativeTime(round.last_activity_at))}` : ''}</span>
        </div>
        <div class="promotion-packet-meta promotion-round-meta">${roundPills.join('')}</div>
        <div class="promotion-round-summary">
          ${summaryStats.map((item) => `
            <div class="promotion-round-stat">
              <div class="label">${escapeHtml(item.label || '')}</div>
              <div class="value ${escapeHtml(item.tone || '')}">${escapeHtml(item.value || '')}</div>
            </div>
          `).join('')}
        </div>
        <div class="promotion-packet-list">
          ${packetList.map((packet) => renderPacket(packet)).join('')}
        </div>
        ${remediationHtml}
      </section>
    `;
  }).join('') : '<div class="empty">No promotion review packets recorded yet.</div>';
  const latestRoundHero = latestRound ? `
    <section class="promotion-latest-round">
      <div class="promotion-section-title">
        <h4>Current promotion round: Round ${escapeHtml(String(latestRound.round_number || '?'))}</h4>
        <span class="pill last-activity-pill" data-timestamp="${escapeHtml(latestRound.last_activity_at || '')}">${latestRound.last_activity_at ? `Last activity ${escapeHtml(formatRelativeTime(latestRound.last_activity_at))}` : ''}</span>
      </div>
      <div class="promotion-state-banner ${latestRound.status === 'passed' ? 'status-complete' : latestRound.status === 'failed' || latestRound.status === 'needs_remediation' ? 'status-failed' : ''}">
        <div class="promotion-state-banner-icon">${
          latestRound.status === 'remediating' ? 'A' :
          latestRound.status === 'ready_for_rerun' ? 'R' :
          latestRound.status === 'running' ? 'P' :
          latestRound.status === 'passed' ? 'Q' :
          '!'
        }</div>
        <div class="promotion-state-banner-copy">
          <strong>${
            latestRound.status === 'remediating' ? 'Atomic is running remediation work' :
            latestRound.status === 'ready_for_rerun' ? 'Ready for the next promotion review round' :
            latestRound.status === 'running' ? 'Promotion review is actively running' :
            latestRound.status === 'passed' ? (latestRound.operator_override ? 'This round was operator-approved' : 'This round passed cleanly') :
            latestRound.status === 'failed' ? 'This review round failed' :
            'This round still needs remediation'
          }</strong>
          <span>${
            latestRound.status === 'remediating' ? 'The harness is back in Atomic right now, working reviewer feedback before the next round.' :
            latestRound.status === 'ready_for_rerun' ? 'All remediation tasks from this round are done. The harness should re-review next.' :
            latestRound.status === 'running' ? 'Reviewer packets will appear here as the board finishes its judgments.' :
            latestRound.status === 'passed' ? (latestRound.operator_override ? 'An operator override cleared this round for promotion even though the underlying reviewer packets remain recorded below.' : 'No reviewer concerns remain for this round.') :
            'Reviewer concerns still require action.'
          }</span>
        </div>
      </div>
      <div class="promotion-round-summary">
        <div class="promotion-round-stat"><div class="label">Pass</div><div class="value status-complete">${escapeHtml(String((latestRound.verdict_counts || {}).pass || 0))}</div></div>
        <div class="promotion-round-stat"><div class="label">Concern</div><div class="value ${((latestRound.verdict_counts || {}).concern || 0) > 0 ? 'status-running' : ''}">${escapeHtml(String((latestRound.verdict_counts || {}).concern || 0))}</div></div>
        <div class="promotion-round-stat"><div class="label">Remediation Tasks</div><div class="value">${escapeHtml(String((latestRound.remediation_counts || {}).total || 0))}</div></div>
        <div class="promotion-round-stat"><div class="label">Open</div><div class="value ${((((latestRound.remediation_counts || {}).active || 0) + ((latestRound.remediation_counts || {}).pending || 0)) > 0) ? 'status-running' : ''}">${escapeHtml(String(((latestRound.remediation_counts || {}).active || 0) + ((latestRound.remediation_counts || {}).pending || 0)))}</div></div>
        <div class="promotion-round-stat"><div class="label">Done</div><div class="value ${((latestRound.remediation_counts || {}).completed || 0) > 0 ? 'status-complete' : ''}">${escapeHtml(String((latestRound.remediation_counts || {}).completed || 0))}</div></div>
        <div class="promotion-round-stat"><div class="label">LLM usage</div><div class="value">${escapeHtml(usageBits(latestUsage).join(' · ') || 'Not recorded')}</div></div>
      </div>
    </section>
  ` : '';
  function verdictEmoji(verdict) {
    const raw = String(verdict || '').trim();
    if (raw === 'pass') return '✅';
    if (raw === 'concern') return '🚩';
    if (raw === 'remediation_required') return '❌';
    return '•';
  }
  const selectedReportKey = state.selectedPromotionReportKey || '';
  const reportCardHtml = latestGradedRound ? `
    <section class="promotion-report-card">
      <div class="promotion-section-title">
        <h4>Round ${escapeHtml(String(latestGradedRound.round_number || '?'))} Report Card</h4>
        <span class="hint">${latestRound && latestRound !== latestGradedRound ? 'Latest graded round while the current round is still gathering reviewer packets.' : 'Latest graded round.'}</span>
      </div>
      <div class="promotion-report-card-grid">
        ${(Array.isArray(latestGradedRound.packets) ? latestGradedRound.packets : []).map((packet) => {
          const reviewer = humanizeReviewer(packet.reviewer, packet.dimension);
          const verdict = String(packet.verdict || '').trim();
          const progress = progressLabel(packet.progress_status || '');
          const reportKey = `${latestGradedRound.review_id || latestGradedRound.round_number}:${packet.dimension || reviewer}`;
          const title = [
            `${reviewer}`,
            `Dimension: ${humanizeDimension(packet.dimension || '')}`,
            `Verdict: ${verdictLabel(verdict)}`,
            progress ? `Progress: ${progress}` : '',
            packet.summary ? `Summary: ${packet.summary}` : '',
          ].filter(Boolean).join('\n');
          return `
            <button type="button" class="promotion-report-card-cell ${selectedReportKey === reportKey ? 'active' : ''}" data-report-key="${escapeHtml(reportKey)}" title="${escapeHtml(title)}">
              <div class="emoji">${escapeHtml(verdictEmoji(verdict))}</div>
              <div class="name">${escapeHtml(reviewer)}</div>
              <div class="sub">${escapeHtml(humanizeDimension(packet.dimension || ''))}</div>
              ${progress ? `<div class="sub">${escapeHtml(progress)}</div>` : ''}
            </button>
          `;
        }).join('')}
      </div>
      ${(() => {
        const packetList = Array.isArray(latestGradedRound.packets) ? latestGradedRound.packets : [];
        const selectedPacket = packetList.find((packet) => `${latestGradedRound.review_id || latestGradedRound.round_number}:${packet.dimension || humanizeReviewer(packet.reviewer, packet.dimension)}` === selectedReportKey) || packetList[0];
        if (!selectedPacket) return '';
        const reviewer = humanizeReviewer(selectedPacket.reviewer, selectedPacket.dimension);
        const progress = progressLabel(selectedPacket.progress_status || '');
        const llmUsage = selectedPacket.llm_usage && typeof selectedPacket.llm_usage === 'object' ? selectedPacket.llm_usage : {};
        const usageSummary = usageBits(llmUsage);
        const usageReported = selectedPacket.llm_usage_reported !== false;
        return `
          <div class="promotion-report-card-detail">
            <div class="promotion-section-title">
              <h4>${escapeHtml(reviewer)}</h4>
              <span class="hint">${escapeHtml(humanizeDimension(selectedPacket.dimension || ''))}</span>
            </div>
            <div class="promotion-packet-meta">
              <span class="pill promotion-verdict-pill ${selectedPacket.verdict === 'pass' ? 'status-complete' : selectedPacket.verdict === 'remediation_required' ? 'status-failed' : 'status-running'}">${escapeHtml(verdictEmoji(selectedPacket.verdict || ''))}</span>
              ${selectedPacket.severity ? `<span class="pill promotion-opinion-pill">${escapeHtml(`Severity: ${selectedPacket.severity}`)}</span>` : ''}
              ${selectedPacket.owner_scope ? `<span class="pill promotion-opinion-pill">${escapeHtml(`Scope: ${selectedPacket.owner_scope}`)}</span>` : ''}
              ${progress ? `<span class="pill ${selectedPacket.progress_status === 'resolved' ? 'status-complete' : selectedPacket.progress_status === 'improving' ? 'status-running' : 'status-failed'}">${escapeHtml(progress)}</span>` : ''}
              ${usageSummary.length ? `<span class="pill">${escapeHtml(usageSummary.join(' · '))}</span>` : (!usageReported ? `<span class="pill status-idle">Usage not reported</span>` : '')}
            </div>
            <div class="promotion-packet-summary">${escapeHtml(selectedPacket.summary || 'No summary recorded.')}</div>
            ${(selectedPacket.closure_criteria || selectedPacket.evidence_required || selectedPacket.repeat_reason)
              ? `<div class="promotion-requirements">
                  ${selectedPacket.closure_criteria ? `<div class="promotion-requirement-block"><div class="label">Closure criteria</div><div class="promotion-requirement-body">${escapeHtml(selectedPacket.closure_criteria)}</div></div>` : ''}
                  ${selectedPacket.evidence_required ? `<div class="promotion-requirement-block"><div class="label">Evidence required</div><div class="promotion-requirement-body">${escapeHtml(selectedPacket.evidence_required)}</div></div>` : ''}
                  ${selectedPacket.repeat_reason ? `<div class="promotion-requirement-block"><div class="label">Why still open</div><div class="promotion-requirement-body">${escapeHtml(selectedPacket.repeat_reason)}</div></div>` : ''}
                </div>`
              : ''
            }
            ${(Array.isArray(selectedPacket.findings) && selectedPacket.findings.length)
              ? `<ul class="promotion-packet-issues">${selectedPacket.findings.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`
              : ''}
            ${(Array.isArray(selectedPacket.evidence) && selectedPacket.evidence.length)
              ? `<ul class="promotion-packet-evidence">${selectedPacket.evidence.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`
              : ''}
          </div>
        `;
      })()}
    </section>
  ` : '';
  const taskPacketHtml = packets.filter((packet) => packet.source !== 'objective_review').length
    ? `
      <section>
        <div class="promotion-section-title">
          <h4>Task-level promotion packets</h4>
          <span class="hint">Affirmation summaries captured on individual task promotions.</span>
        </div>
        <div class="promotion-packet-list">${packets.filter((packet) => packet.source !== 'objective_review').map((packet) => renderPacket(packet)).join('')}</div>
      </section>
    `
    : '';
  const failedHtml = failedTasks.length ? failedTasks.map((task) => {
    const disposition = task.disposition || {};
    const waiver = task.waiver || {};
    const effective = task.effective_status || 'blocking';
    return `
      <article class="promotion-failed-task">
        <div class="label">Failed task</div>
        <div class="value">${escapeHtml(task.title || task.task_id || 'Failed task')}</div>
        <div class="promotion-failed-meta">
          <span class="pill ${effective === 'waived' ? 'status-complete' : 'status-failed'}">${escapeHtml(effective)}</span>
          ${disposition.kind ? `<span class="pill">Disposition: ${escapeHtml(disposition.kind)}</span>` : ''}
          ${waiver.created_at ? `<span class="pill">Resolved ${escapeHtml(formatRelativeTime(waiver.created_at))}</span>` : ''}
        </div>
        <div class="promotion-failed-body">${escapeHtml(waiver.rationale || task.objective || 'No failure rationale recorded.')}</div>
      </article>
    `;
  }).join('') : '<div class="empty">No failed tasks are currently recorded for this objective.</div>';
  const promotionSucceeded = latestRepoPromotion?.status === 'approved' && applyback?.status === 'applied';
  const successHeadline = projectSettings.promotion_mode === 'direct_main'
    ? `Pushed directly to ${projectSettings.base_branch || 'main'}`
    : applyback?.pr_url
      ? 'Pushed branch and opened pull request'
      : 'Pushed branch to remote';
  const successBody = projectSettings.promotion_mode === 'direct_main'
    ? `The harness built the current objective snapshot, committed it in an isolated promotion worktree, pushed it to origin/${projectSettings.base_branch || 'main'} for ${projectSettings.repo_name || 'the configured repo'}, and cleaned up the transient worktree after verification.`
    : applyback?.pr_url
      ? `The harness built the current objective snapshot, pushed ${applyback.pushed_ref || applyback.branch_name || 'the branch'} to origin, and opened a review against ${projectSettings.base_branch || 'main'}.`
      : `The harness built the current objective snapshot and pushed ${applyback.pushed_ref || applyback.branch_name || 'the branch'} to origin for ${projectSettings.repo_name || 'the configured repo'}.`;
  const repoPromotionHtml = `
    <section>
        <div class="promotion-section-title">
          <h4>Promote to repo</h4>
        <span class="hint">Objective review clears governance. This action builds an objective snapshot from all tracked objective files, commits it in an isolated promotion worktree, pushes it, and then cleans up the transient worktree after verification.</span>
      </div>
      <div class="promotion-summary-card ${promotionSucceeded ? 'repo-promotion-success' : ''}">
        ${promotionSucceeded ? `
          <h5 class="repo-promotion-success-title">${escapeHtml(successHeadline)}</h5>
          <div class="repo-promotion-success-copy">${escapeHtml(successBody)}</div>
          <div class="repo-promotion-success-meta">
            ${applyback.commit_sha ? `<span class="pill status-complete">Commit ${escapeHtml(String(applyback.commit_sha).slice(0, 8))}</span>` : ''}
            ${projectSettings.promotion_mode === 'direct_main' ? `<span class="pill status-complete">Remote updated: origin/${escapeHtml(projectSettings.base_branch || 'main')}</span>` : ''}
            ${applyback.pushed_ref && projectSettings.promotion_mode !== 'direct_main' ? `<span class="pill status-complete">Remote updated: origin/${escapeHtml(applyback.pushed_ref)}</span>` : ''}
            ${applyback.pr_url ? `<a class="repo-promotion-success-link" href="${escapeHtml(applyback.pr_url)}" target="_blank" rel="noreferrer">Open review</a>` : ''}
          </div>
        ` : ''}
        <div class="label">Objective snapshot</div>
        <div class="value" style="font-size:1rem;">${escapeHtml(candidate?.title || 'No completed linked task')}</div>
        <div class="helper">
          ${candidate ? `Anchor task ${escapeHtml(candidate.task_id || '')} · run ${escapeHtml(candidate.latest_completed_run_id || '')} · attempt ${escapeHtml(String(candidate.latest_completed_attempt || '?'))}` : 'No completed linked task is available for this objective yet.'}
        </div>
        <div class="helper" style="margin-top:10px;">${escapeHtml(repoPromotion.reason || '')}</div>
        ${latestRepoPromotion ? `<div class="promotion-failed-meta" style="margin-top:12px;">
          <span class="pill ${latestRepoPromotion.status === 'approved' ? 'status-complete' : latestRepoPromotion.status === 'pending' ? '' : 'status-failed'}">Latest promotion: ${escapeHtml(latestRepoPromotion.status || '')}</span>
          ${latestRepoPromotion.applyback?.pr_url ? `<span class="pill">PR ready</span>` : ''}
          ${latestRepoPromotion.applyback?.commit_sha ? `<span class="pill">Commit ${escapeHtml(String(latestRepoPromotion.applyback.commit_sha).slice(0, 8))}</span>` : ''}
        </div>` : ''}
        <div class="promotion-action-row" style="margin-top:14px;">
          <button type="button" class="promotion-primary-button" ${repoPromotion.eligible ? '' : 'disabled'} data-promotion-action="promote-objective-to-repo">Promote Objective to The Repo</button>
        </div>
      </div>
    </section>
  `;
  promotionReviewContent.innerHTML = `
    ${latestRoundHero}
    ${repoPromotionHtml}
    ${reportCardHtml}
    ${summaryCards}
    ${taskPacketHtml}
  `;
  promotionReviewRoundsContent.innerHTML = `
    <section>
      <div class="promotion-section-title">
        <h4>Objective review rounds</h4>
        <span class="hint">Each automatic promotion-review round is grouped separately so you can compare findings before and after remediation.</span>
      </div>
      <div class="promotion-round-list">${roundHtml}</div>
    </section>
    <section>
      <div class="promotion-section-title">
        <h4>Failed task dispositions</h4>
        <span class="hint">Historical failures stay visible here even when waived.</span>
      </div>
      <div class="promotion-failed-list">${failedHtml}</div>
    </section>
  `;
  promotionReviewContent.querySelectorAll('[data-report-key]').forEach((element) => {
    element.addEventListener('click', () => {
      state.selectedPromotionReportKey = element.getAttribute('data-report-key') || '';
      renderPromotionReview();
    });
  });
  promotionReviewContent.querySelectorAll('[data-promotion-action]').forEach((element) => {
    element.addEventListener('click', async () => {
      const action = element.getAttribute('data-promotion-action') || '';
      if (action === 'save-repo-settings') {
        await handleSaveRepoPromotionSettings();
      } else if (action === 'promote-objective-to-repo') {
        await handlePromoteObjectiveToRepo();
      }
    });
  });
}

async function handleForcePromotionOverride() {
  if (!state.objectiveId) return;
  const rationale = window.prompt('Why are you force-promoting this objective review? This will be recorded in the audit trail.', 'Operator override: promotion review evidence is sufficient and remaining blocker is harness bookkeeping.');
  if (rationale === null) return;
  const trimmed = rationale.trim();
  if (!trimmed) {
    showError('Enter a rationale before force-promoting.');
    return;
  }
  try {
    clearError();
    await api(`/api/objectives/${encodeURIComponent(state.objectiveId)}/promotion/force`, {
      method: 'POST',
      body: JSON.stringify({ rationale: trimmed }),
    });
    await loadWorkspace();
  } catch (error) {
    showError(error.message || 'Unable to force-promote the objective review');
  }
}

async function handleSaveRepoPromotionSettings() {
  if (!state.projectId) return;
  const promotionMode = document.getElementById('settings-repo-promotion-mode')?.value || '';
  const repoProvider = document.getElementById('settings-repo-provider')?.value || '';
  const repoName = document.getElementById('settings-repo-name')?.value || '';
  const baseBranch = document.getElementById('settings-repo-base-branch')?.value || '';
  try {
    clearError();
    state.repoSettingsSaving = true;
    syncRepoSettingsButtonState();
    await api(`/api/projects/${encodeURIComponent(state.projectId)}/repo-settings`, {
      method: 'POST',
      body: JSON.stringify({
        promotion_mode: promotionMode,
        repo_provider: repoProvider,
        repo_name: repoName,
        base_branch: baseBranch,
      }),
    });
    state.repoSettingsSaving = false;
    state.repoSettingsSavedAt = Date.now();
    await loadWorkspace();
  } catch (error) {
    state.repoSettingsSaving = false;
    syncRepoSettingsButtonState();
    showError(error.message || 'Unable to save repo promotion settings');
  }
}

async function handlePromoteObjectiveToRepo() {
  if (!state.objectiveId) return;
  const objective = currentObjective();
  const repoPromotion = objective?.repo_promotion || {};
  const candidate = repoPromotion.candidate || null;
  const settings = repoPromotion.project_settings || {};
  const destination = settings.promotion_mode === 'direct_main'
    ? `push directly to ${settings.base_branch || 'main'}`
    : settings.promotion_mode === 'branch_only'
      ? 'push a branch without opening a PR'
      : `push a branch and open a PR to ${settings.base_branch || 'main'}`;
  const candidateText = candidate
    ? `${candidate.title} (${candidate.task_id}, run ${candidate.latest_completed_run_id})`
    : 'the objective snapshot';
  const confirmed = await openModal({
    title: 'Promote Objective to The Repo',
    body: `Promote the current objective snapshot anchored by ${candidateText}?\n\nThis will ${destination} for ${settings.repo_name || 'the configured repo'}.`,
    confirmLabel: 'Promote now',
    cancelLabel: 'Cancel',
  });
  if (!confirmed) return;
  try {
    clearError();
    setModalWorking({
      title: 'Promoting Objective to The Repo',
      body: `The harness is staging the current objective snapshot in an isolated promotion worktree, applying it against the latest ${settings.base_branch || 'main'}, and pushing it. You will stay here until the result is ready.`,
      statusText: settings.promotion_mode === 'direct_main'
        ? `Pushing to origin/${settings.base_branch || 'main'}…`
        : 'Preparing repository promotion…',
    });
    const result = await api(`/api/objectives/${encodeURIComponent(state.objectiveId)}/promote`, {
      method: 'POST',
      body: JSON.stringify({}),
    });
    const applyback = result?.applyback || {};
    const successText = settings.promotion_mode === 'direct_main'
      ? `The harness committed the objective snapshot, pushed it to origin/${settings.base_branch || 'main'}, verified the remote update, and removed the transient promotion worktree.`
      : applyback.pr_url
        ? `The harness committed the objective snapshot, pushed ${applyback.pushed_ref || applyback.branch_name || 'the branch'} to origin, and opened a review.`
        : `The harness committed the objective snapshot and pushed ${applyback.pushed_ref || applyback.branch_name || 'the branch'} to origin.`;
    await loadWorkspace();
    await openModal({
      title: 'Promotion completed',
      body: successText,
      confirmLabel: 'Close',
      tone: 'success',
      showCancel: false,
    });
  } catch (error) {
    const message = error.message || 'Unable to promote objective code to the repository';
    showError(message);
    await openModal({
      title: 'Promotion did not complete',
      body: message,
      confirmLabel: 'Close',
      tone: 'error',
      showCancel: false,
    });
  }
}

function renderInterrogationReview(objective) {
  const review = objective?.interrogation_review || null;
  if (!review) {
    interrogationSummary.textContent = 'No interrogation review is available yet.';
    interrogationPlan.innerHTML = '';
    interrogationQuestions.innerHTML = '';
    interrogationCompleteButton.hidden = true;
    return;
  }
  interrogationSummary.textContent = review.completed
    ? (review.summary || 'Interrogation and self-red-team are complete.')
    : 'The harness should clarify the objective and poke holes in the initial plan before Mermaid review.';
  interrogationPlan.innerHTML = (review.plan_elements || []).map((item) => `<li>${escapeHtml(item)}</li>`).join('');
  interrogationQuestions.innerHTML = (review.questions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join('');
  interrogationCompleteButton.hidden = true;
}

function renderRuns() {
  runList.innerHTML = '';
  const workspace = state.workspace;
  const task = workspace?.tasks.find((item) => item.id === state.taskId);
  if (!task || task.runs.length === 0) {
    runList.innerHTML = '<div class="empty">No runs for this task.</div>';
    return;
  }
  for (const run of [...task.runs].reverse()) {
    const button = document.createElement('button');
    button.className = run.id === state.runId ? 'active' : '';
    button.innerHTML = `<span class="title">Attempt ${run.attempt}</span><span class="meta">${run.status} · ${run.id}</span>`;
    button.addEventListener('click', () => {
      state.runId = run.id;
      loadRunOutput();
      renderRuns();
    });
    runList.appendChild(button);
  }
}

async function renderDiagram() {
  const objective = currentObjective();
  const code = objective?.diagram_proposal?.content || objective?.diagram?.content || state.workspace?.diagram?.mermaid || '';
  if (!code) {
    diagramShell.innerHTML = '<div class="empty">No control-flow diagram available yet.</div>';
    return;
  }
  try {
    const mermaidInstance = await ensureMermaid();
    const id = `diagram-${Math.random().toString(36).slice(2)}`;
    const rendered = await mermaidInstance.render(id, code);
    diagramShell.innerHTML = rendered.svg;
    diagramShell.classList.toggle('updating', state.diagramUpdating);
    diagramShell.classList.toggle('locked', Boolean(objective?.diagram && !objective?.diagram_proposal && objective.diagram.status === 'finished'));
    applyDiagramTransform();
    diagramShell.querySelectorAll('svg .node, svg .edgeLabel, svg .label, svg .cluster, svg text').forEach((node) => {
      if (node instanceof SVGElement) {
        node.dataset.clickableMermaid = '1';
      }
    });
    if (state.diagramAnchor) {
      const candidates = Array.from(diagramShell.querySelectorAll('[data-clickable-mermaid="1"]'));
      const match = candidates.find((node) => (node.textContent || '').replace(/\s+/g, ' ').trim() === state.diagramAnchor.label);
      if (match instanceof SVGElement) {
        setDiagramAnchor(state.diagramAnchor, match);
      }
    }
  } catch (error) {
    diagramShell.innerHTML = `<div class="error">${escapeHtml(error.message || 'Failed to render diagram')}</div>`;
  }
}

function renderOutput() {
  const activeTabs = (!cliPanel.hidden && state.expandAll) ? outputTabs : inlineOutputTabs;
  const activeBody = (!cliPanel.hidden && state.expandAll) ? outputBody : inlineOutputBody;
  outputTabs.innerHTML = '';
  inlineOutputTabs.innerHTML = '';
  const sections = state.runOutput?.sections || [];
  const summary = state.runOutput?.summary || null;
  if (summary) {
    inlineOutputSummary.hidden = false;
    inlineOutputSummaryBody.innerHTML = `
      <div><div class="label">What happened</div><div class="value">${escapeHtml(summary.headline)}</div></div>
      <div><div class="label">Why it matters</div><div class="body">${escapeHtml(summary.interpretation)}</div></div>
      <div><div class="label">Recommended next step</div><div class="body">${escapeHtml(summary.recommended_next)}</div></div>
      ${summary.highlights?.length ? `<div><div class="label">Evidence highlights</div><ul class="summary-list">${summary.highlights.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul></div>` : ''}
    `;
  } else {
    inlineOutputSummary.hidden = true;
    inlineOutputSummaryBody.innerHTML = '';
  }
  if (sections.length === 0) {
    outputBody.textContent = 'No readable run evidence for this attempt yet.';
    inlineOutputBody.textContent = 'No readable run evidence for this attempt yet.';
    inlineOutputToggle.hidden = true;
    inlineOutputTabs.hidden = true;
    inlineOutputBody.hidden = true;
    inlineOutputPanel.hidden = !(state.showInlineReview && summary);
    return;
  }
  inlineOutputToggle.hidden = false;
  if (!state.activeSectionPath || !sections.find((item) => item.path === state.activeSectionPath)) {
    state.activeSectionPath = sections[0].path;
  }
  for (const section of sections) {
    const button = document.createElement('button');
    button.textContent = section.label;
    button.className = section.path === state.activeSectionPath ? 'active' : '';
    button.addEventListener('click', () => {
      state.activeSectionPath = section.path;
      renderOutput();
    });
    activeTabs.appendChild(button);
  }
  const active = sections.find((item) => item.path === state.activeSectionPath) || sections[0];
  activeBody.textContent = active.content;
  if (activeTabs !== outputTabs) {
    outputBody.textContent = active.content;
  } else {
    inlineOutputBody.textContent = active.content;
  }
}

function renderWorkspaceChrome() {
  const workspace = state.workspace;
  if (!workspace) return;
  const objective = currentObjective();
  if (state.view === 'objective-create') {
    workspaceTitle.textContent = 'Create a new objective';
    workspaceSummary.textContent = 'Start a new durable workstream with a dedicated title and optional summary.';
  } else if (state.view === 'token-performance') {
    workspaceTitle.textContent = 'Token performance';
    workspaceSummary.textContent = 'Review LLM usage across promotion-review rounds, objectives, and reviewers.';
  } else if (state.view === 'settings') {
    workspaceTitle.textContent = 'Project settings';
    workspaceSummary.textContent = 'Repository promotion policy for the currently selected project.';
  } else {
    workspaceTitle.textContent = objective ? objective.title : workspace.project.name;
    workspaceSummary.textContent = objective
      ? (objective.summary || 'No objective summary recorded.')
      : (workspace.project.description || 'No project summary recorded.');
  }
  const queueDepth = Number(workspace.loop_status.queue_depth || 0);
  if (state.view === 'atomic' || state.view === 'promotion-review') {
    if (queueDepth > 0) {
      workspaceStatus.textContent = `Other project work: ${queueDepth} queued`;
    } else {
      workspaceStatus.textContent = `No other project work queued`;
    }
  } else if (state.view === 'objective-create') {
    workspaceStatus.textContent = state.projectId
      ? `Creating in ${workspace.project.name}`
      : 'Select a project';
  } else if (state.view === 'token-performance') {
    workspaceStatus.textContent = state.projectId
      ? `Usage in ${workspace.project.name}`
      : 'Select a project';
  } else if (state.view === 'settings') {
    workspaceStatus.textContent = state.projectId
      ? `Editing ${workspace.project.name}`
      : 'Select a project';
  } else {
    workspaceStatus.textContent = `${workspace.loop_status.status} · queue ${queueDepth}`;
  }
  renderViewNav();
}

function renderViewNav() {
  if (!viewNav) return;
  const params = new URLSearchParams();
  if (state.projectId) params.set('project_id', state.projectId);
  if (state.objectiveId) params.set('objective_id', state.objectiveId);
  const suffix = params.toString() ? `?${params.toString()}` : '';
  const links = [
    { key: 'control-flow', href: `/workspace${suffix}`, label: 'Control Flow' },
    { key: 'atomic', href: `/atomic${suffix}`, label: 'Atomic' },
    { key: 'promotion-review', href: `/promotion-review${suffix}`, label: 'Promotion Review' },
    { key: 'settings', href: `/settings${suffix}`, label: 'Settings' },
    { key: 'token-performance', href: `/token-performance${suffix}`, label: 'Token Performance' },
    { key: 'harness', href: '/harness', label: 'Dashboard' },
  ];
  viewNav.innerHTML = links.map((link) => (
    `<a class="view-nav-link ${state.view === link.key ? 'active' : ''}" data-view-key="${link.key}" href="${link.href}">${escapeHtml(link.label)}</a>`
  )).join('');
}

function objectiveCreateHref() {
  const params = new URLSearchParams();
  if (state.projectId) params.set('project_id', state.projectId);
  const suffix = params.toString() ? `?${params.toString()}` : '';
  return `/objectives/new${suffix}`;
}

async function loadProjects() {
  const payload = await api('/api/projects');
  state.projects = payload.projects;
  const urlParams = new URLSearchParams(window.location.search);
  const preferredProjectId = urlParams.get('project_id');
  const preferredObjectiveId = urlParams.get('objective_id');
  if (preferredProjectId && state.projects.some((project) => project.id === preferredProjectId)) {
    setProjectId(preferredProjectId);
  }
  if (preferredObjectiveId) {
    setObjectiveId(preferredObjectiveId);
  }
  if (!state.projectId && state.projects.length > 0) {
    setProjectId(preferredProjectFromList(state.projects));
  }
  if (state.projectId && !state.projects.some((project) => project.id === state.projectId)) {
    setProjectId(preferredProjectFromList(state.projects));
  }
  renderProjects();
}

async function loadWorkspace() {
  if (!state.projectId) return;
  clearError();
  state.workspace = await api(`/api/projects/${encodeURIComponent(state.projectId)}/workspace`);
  setObjectiveId(
    state.objectiveId && state.workspace.objectives.find((item) => item.id === state.objectiveId)
      ? state.objectiveId
      : (state.workspace.objectives[0] || {}).id || null
  );
  state.taskId = state.taskId && state.workspace.tasks.find((item) => item.id === state.taskId)
    ? state.taskId
    : (state.workspace.tasks[0] || {}).id || null;
  state.runId = state.runId && state.workspace.tasks.some((item) => item.runs.some((run) => run.id === state.runId))
    ? state.runId
    : pickDefaultRun(state.workspace);
  renderObjectives();
  renderWorkspaceChrome();
  renderObjectiveCreatePage();
  renderSettingsPage();
  renderTokenPerformancePage();
  renderTasks();
  renderRuns();
  renderExecutionPanel();
  renderPromotionReview();
  maybeFollowRecommendedView();
  await renderDiagram();
  await loadRunOutput();
}

async function loadRunOutput() {
  if (!state.runId) {
    state.runOutput = { sections: [] };
    renderOutput();
    return;
  }
  state.runOutput = await api(`/api/runs/${encodeURIComponent(state.runId)}/cli-output`);
  renderOutput();
}

async function handleExecutionPrimaryAction() {
  const objective = currentObjective();
  if (!objective) return;
  const linkedTasks = (state.workspace?.tasks || []).filter((task) => task.objective_id === objective.id);
  const task = linkedTasks[0] || null;
  if (!task) return;
  const action = conversationPrimaryButton.dataset.action || executionPrimaryButton.dataset.action || '';
  clearError();
  if (action === 'start-run') {
    state.suppressFocusAnimation = true;
    await api(`/api/tasks/${encodeURIComponent(task.id)}/run`, {
      method: 'POST',
    });
    await loadWorkspace();
    state.suppressFocusAnimation = false;
    conversationInput.focus();
    return;
  }
  if (action === 'review-run') {
    state.taskId = task.id;
      state.runId = ([...task.runs].reverse()[0] || {}).id || null;
      state.suppressFocusAnimation = true;
      state.manualFocusMode = null;
      state.showInlineReview = true;
      renderTasks();
      renderRuns();
      await loadRunOutput();
      applyFocusMode(currentObjective());
      inlineOutputPanel.scrollIntoView({behavior: 'smooth', block: 'nearest'});
      state.suppressFocusAnimation = false;
  }
}

if (createObjectiveForm) {
  createObjectiveForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    await submitObjectiveCreate(createObjectiveTitle.value.trim(), createObjectiveSummary.value.trim());
  });
}

async function submitObjectiveCreate(title, summary) {
  if (!state.projectId) {
    showError('Select a project before creating an objective.');
    return;
  }
  if (!title) {
    showError('Enter an objective title before creating it.');
    return;
  }
  try {
    clearError();
    const payload = await api(`/api/projects/${encodeURIComponent(state.projectId)}/objectives`, {
      method: 'POST',
      body: JSON.stringify({
        title,
        summary,
      }),
    });
    createObjectiveTitle.value = '';
    createObjectiveSummary.value = '';
    if (pageCreateObjectiveTitle) pageCreateObjectiveTitle.value = '';
    if (pageCreateObjectiveSummary) pageCreateObjectiveSummary.value = '';
    setObjectiveId(payload.objective.id);
    await loadWorkspace();
    const params = new URLSearchParams();
    if (state.projectId) params.set('project_id', state.projectId);
    if (payload.objective?.id) params.set('objective_id', payload.objective.id);
    window.location.assign(`/workspace?${params.toString()}`);
  } catch (error) {
    showError(error.message || 'Unable to create objective');
  }
}

if (headerCreateObjective) {
  headerCreateObjective.addEventListener('click', () => {
    if (!state.projectId) {
      showError('Select a project before creating an objective.');
      return;
    }
    window.location.assign(objectiveCreateHref());
  });
}

if (pageCreateObjectiveForm) {
  pageCreateObjectiveForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const selectedProjectId = pageCreateObjectiveProject?.value || state.projectId;
    const title = pageCreateObjectiveTitle?.value.trim() || '';
    const summary = pageCreateObjectiveSummary?.value.trim() || '';
    if (!selectedProjectId) {
      showError('Select a project before creating an objective.');
      return;
    }
    if (!title) {
      showError('Enter an objective title before creating it.');
      return;
    }
    setProjectId(selectedProjectId);
    await submitObjectiveCreate(title, summary);
  });
}

if (conversationForm) {
  conversationForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!state.objectiveId) return;
    if (state.conversationPending) return;
    const text = conversationInput.value.trim();
    if (!text) return;
    const mode = currentFocusMode(currentObjective());
    const model = currentIntentModel();
    let completed = false;
    const diagramAction = likelyMermaidActionIntent(text);
    const outboundText = state.diagramAnchor
      ? `[Mermaid anchor: ${state.diagramAnchor.label}] ${text}`
      : text;
    try {
      clearError();
      setConversationPending(true);
      setDiagramUpdating(diagramAction);
      if (diagramAction) {
        addLocalNotice('System receipt: updating Mermaid proposal...');
        renderConversationTranscript(currentObjective());
      }
      activeConversationController = new AbortController();
      if (['desired_outcome', 'success_definition', 'non_negotiables'].includes(mode)) {
        state.lastSavedStep = mode;
        state.manualFocusMode = null;
        await api(`/api/objectives/${encodeURIComponent(state.objectiveId)}/intent`, {
          method: 'PUT',
          signal: activeConversationController.signal,
          body: JSON.stringify({
            intent_summary: mode === 'desired_outcome' ? outboundText : (model?.intent_summary || ''),
            success_definition: mode === 'success_definition' ? outboundText : (model?.success_definition || ''),
            non_negotiables: mode === 'non_negotiables' ? outboundText.split('\\n') : (model?.non_negotiables || []),
            frustration_signals: model?.frustration_signals || [],
          }),
        });
        await loadWorkspace();
        completed = true;
        return;
      }
      await api(`/api/projects/${encodeURIComponent(state.projectId)}/comments`, {
        method: 'POST',
        signal: activeConversationController.signal,
        body: JSON.stringify({
          author: '',
          text: outboundText,
          objective_id: state.objectiveId,
        }),
      });
      conversationInput.value = '';
      clearDiagramAnchor();
      state.suppressFocusAnimation = true;
      state.showInlineReview = false;
      await loadWorkspace();
      completed = true;
    } catch (error) {
      if (error.name === 'AbortError') {
        addLocalNotice('System receipt: interrupted by operator before the response completed.');
        renderConversationTranscript(currentObjective());
        showError('Interrupted. No further response will be applied from that request.');
      } else {
        showError(error.message || 'Unable to send message to the harness');
      }
    } finally {
      state.suppressFocusAnimation = false;
      setConversationPending(false);
      setDiagramUpdating(false);
      activeConversationController = null;
      if (completed) {
        conversationInput.focus();
      }
    }
  });
}

if (conversationInterrupt) {
  conversationInterrupt.addEventListener('click', () => {
    if (activeConversationController) {
      activeConversationController.abort();
    }
  });
}

if (diagramCommentAnchorClear) {
  diagramCommentAnchorClear.addEventListener('click', () => {
    clearDiagramAnchor();
    conversationInput.focus();
  });
}

if (diagramShell) {
diagramShell.addEventListener('click', (event) => {
  if (state.diagramPan.isDragging) {
    state.diagramPan.isDragging = false;
    diagramShell.classList.remove('panning');
    return;
  }
  const objective = currentObjective();
  if (!objective || !objective.diagram) return;
  const target = findDiagramAnchorTarget(event.target);
  if (!target) return;
  const anchor = anchorFromElement(target);
  if (!anchor) return;
  setDiagramAnchor(anchor, target);
  if (!conversationInput.value.trim()) {
    conversationInput.value = `Change this part of the diagram: `;
  }
  conversationInput.focus();
});

diagramShell.addEventListener('wheel', (event) => {
  const svg = diagramShell.querySelector('svg');
  if (!svg) return;
  event.preventDefault();
  const rect = diagramShell.getBoundingClientRect();
  const pointerX = event.clientX - rect.left;
  const pointerY = event.clientY - rect.top;
  const currentScale = state.diagramPan.scale;
  const zoomFactor = event.deltaY < 0 ? 1.1 : 0.9;
  const nextScale = Math.min(3.5, Math.max(0.35, currentScale * zoomFactor));
  if (nextScale === currentScale) return;
  const worldX = (pointerX - state.diagramPan.x) / currentScale;
  const worldY = (pointerY - state.diagramPan.y) / currentScale;
  state.diagramPan.scale = nextScale;
  state.diagramPan.x = pointerX - worldX * nextScale;
  state.diagramPan.y = pointerY - worldY * nextScale;
  applyDiagramTransform();
}, { passive: false });

diagramShell.addEventListener('pointerdown', (event) => {
  if (event.button !== 0) return;
  const svg = diagramShell.querySelector('svg');
  if (!svg) return;
  const onClickableTarget = isClickableMermaidTarget(event.target);
  const panAllowed = event.shiftKey || !onClickableTarget;
  if (!panAllowed) {
    state.diagramPan.isPointerDown = false;
    state.diagramPan.isDragging = false;
    return;
  }
  state.diagramPan.isPointerDown = true;
  state.diagramPan.isDragging = false;
  state.diagramPan.startX = event.clientX;
  state.diagramPan.startY = event.clientY;
  state.diagramPan.dragOriginX = state.diagramPan.x;
  state.diagramPan.dragOriginY = state.diagramPan.y;
  if (diagramShell.setPointerCapture) {
    try {
      diagramShell.setPointerCapture(event.pointerId);
    } catch (_error) {
      // Ignore pointer capture failures.
    }
  }
});

diagramShell.addEventListener('pointermove', (event) => {
  if (!state.diagramPan.isPointerDown) return;
  const dx = event.clientX - state.diagramPan.startX;
  const dy = event.clientY - state.diagramPan.startY;
  if (!state.diagramPan.isDragging && Math.hypot(dx, dy) > 6) {
    state.diagramPan.isDragging = true;
  }
  if (!state.diagramPan.isDragging) return;
  state.diagramPan.x = state.diagramPan.dragOriginX + dx;
  state.diagramPan.y = state.diagramPan.dragOriginY + dy;
  applyDiagramTransform();
});

function endDiagramPointer(event) {
  if (state.diagramPan.isPointerDown && diagramShell.releasePointerCapture) {
    try {
      diagramShell.releasePointerCapture(event.pointerId);
    } catch (_error) {
      // Ignore pointer capture failures.
    }
  }
  state.diagramPan.isPointerDown = false;
}

diagramShell.addEventListener('pointerup', endDiagramPointer);
diagramShell.addEventListener('pointercancel', endDiagramPointer);
}

if (intentForm) {
  intentForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!state.objectiveId) return;
    try {
      clearError();
      state.lastSavedStep = currentFocusMode(currentObjective());
      state.manualFocusMode = null;
      await api(`/api/objectives/${encodeURIComponent(state.objectiveId)}/intent`, {
        method: 'PUT',
        body: JSON.stringify({
          intent_summary: intentSummary.value,
          success_definition: successDefinition.value,
          non_negotiables: nonNegotiables.value.split('\\n'),
          frustration_signals: frustrationSignals.value.split('\\n'),
        }),
      });
      await loadWorkspace();
    } catch (error) {
      showError(error.message || 'Unable to save intent model');
    }
  });
}

async function handleMermaidAction(action) {
  if (!action || !state.objectiveId) return;
  try {
    clearError();
    const proposal = currentObjective()?.diagram_proposal || null;
    if (action === 'accept-proposal' && proposal) {
      await api(`/api/objectives/${encodeURIComponent(state.objectiveId)}/mermaid/proposal/accept`, {
        method: 'POST',
        body: JSON.stringify({ proposal_id: proposal.id }),
      });
      state.showInlineReview = false;
      await loadWorkspace();
      if (state.view === 'control-flow') {
        const params = new URLSearchParams();
        if (state.projectId) params.set('project_id', state.projectId);
        if (state.objectiveId) params.set('objective_id', state.objectiveId);
        window.location.assign(`/atomic?${params.toString()}`);
      }
      return;
    }
    if (action === 'refine-proposal' && proposal) {
      await api(`/api/objectives/${encodeURIComponent(state.objectiveId)}/mermaid/proposal/reject`, {
        method: 'POST',
        body: JSON.stringify({ proposal_id: proposal.id, resolution: 'refine' }),
      });
      state.showInlineReview = false;
      await loadWorkspace();
      return;
    }
    if (action === 'rewind-proposal' && proposal) {
      await api(`/api/objectives/${encodeURIComponent(state.objectiveId)}/mermaid/proposal/reject`, {
        method: 'POST',
        body: JSON.stringify({ proposal_id: proposal.id, resolution: 'rewind_hard' }),
      });
      state.showInlineReview = false;
      await loadWorkspace();
      return;
    }
    const payloadByAction = {
      finished: {
        status: 'finished',
        summary: 'Workflow accepted for execution',
        blocking_reason: '',
      },
      paused: {
        status: 'paused',
        summary: 'Workflow needs more review',
        blocking_reason: 'The current process diagram does not yet match the intended flow.',
      },
    };
    const payload = payloadByAction[action];
    if (!payload) return;
    await api(`/api/objectives/${encodeURIComponent(state.objectiveId)}/mermaid`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    state.showInlineReview = false;
    await loadWorkspace();
    if (action === 'finished' && state.view === 'control-flow') {
      const params = new URLSearchParams();
      if (state.projectId) params.set('project_id', state.projectId);
      if (state.objectiveId) params.set('objective_id', state.objectiveId);
      window.location.assign(`/atomic?${params.toString()}`);
    }
  } catch (error) {
    showError(error.message || 'Unable to update Mermaid review state');
  }
}

if (mermaidControls) {
  mermaidControls.addEventListener('click', async (event) => {
    const action = event.target?.dataset?.mermaidAction;
    await handleMermaidAction(action);
  });
}

if (proposalActions) {
  proposalActions.addEventListener('click', async (event) => {
    const action = event.target?.dataset?.mermaidAction;
    if (action === 'review-run' || action === 'start-run') {
      try {
        await handleExecutionPrimaryAction();
      } catch (error) {
        state.suppressFocusAnimation = false;
        showError(error.message || 'Unable to continue execution from the UI');
      }
      return;
    }
    await handleMermaidAction(action);
  });
}

if (interrogationCompleteButton) {
  interrogationCompleteButton.addEventListener('click', async () => {
    if (!state.objectiveId) return;
    try {
      clearError();
      await api(`/api/objectives/${encodeURIComponent(state.objectiveId)}/interrogation`, {
        method: 'POST',
      });
      await loadWorkspace();
    } catch (error) {
      showError(error.message || 'Unable to complete interrogation review');
    }
  });
}

if (executionPrimaryButton) {
  executionPrimaryButton.addEventListener('click', async () => {
    try {
      await handleExecutionPrimaryAction();
    } catch (error) {
      state.suppressFocusAnimation = false;
      showError(error.message || 'Unable to continue execution from the UI');
    }
  });
}

if (promotionReviewPanel) {
  promotionReviewPanel.addEventListener('click', async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.id !== 'promotion-force-approve-btn') return;
    await handleForcePromotionOverride();
  });
}

if (conversationPrimaryButton) {
  conversationPrimaryButton.addEventListener('click', async () => {
    try {
      await handleExecutionPrimaryAction();
    } catch (error) {
      state.suppressFocusAnimation = false;
      showError(error.message || 'Unable to continue execution from the UI');
    }
  });
}

if (inlineOutputToggle) {
  inlineOutputToggle.addEventListener('click', () => {
    const hidden = inlineOutputBody.hidden;
    inlineOutputBody.hidden = !hidden;
    inlineOutputTabs.hidden = !hidden;
    inlineOutputToggle.textContent = hidden ? 'Hide raw evidence' : 'Show raw evidence';
  });
}

async function handleProjectSelection(projectId) {
  setProjectId(projectId);
  setObjectiveId(null);
  state.taskId = null;
  state.runId = null;
  state.manualFocusMode = null;
  state.showInlineReview = false;
  setSidebarCollapsed(true);
  await loadWorkspace();
}

if (projectSelect) {
  projectSelect.addEventListener('change', async () => {
    await handleProjectSelection(projectSelect.value);
  });
}

if (bannerProjectSelect) {
  bannerProjectSelect.addEventListener('change', async () => {
    await handleProjectSelection(bannerProjectSelect.value);
  });
}

if (atomicObjectiveSelect) {
  atomicObjectiveSelect.addEventListener('change', async () => {
    setObjectiveId(atomicObjectiveSelect.value);
    state.taskId = null;
    state.runId = null;
    state.showInlineReview = false;
    await loadWorkspace();
  });
}

if (sidebarToggle) {
  sidebarToggle.addEventListener('click', () => {
    setSidebarCollapsed(!state.sidebarCollapsed);
  });
}

if (viewNav) {
  viewNav.addEventListener('click', (event) => {
    const link = event.target.closest('[data-view-key]');
    if (!link) return;
    const targetView = link.dataset.viewKey || '';
    const recommended = currentRecommendedView();
    if (targetView && recommended && targetView !== recommended) {
      state.manualViewOverride = targetView;
      sessionStorage.setItem('accruvia.ui.manualViewOverride', targetView);
    } else {
      state.manualViewOverride = '';
      sessionStorage.removeItem('accruvia.ui.manualViewOverride');
    }
  });
}

if (modalCancel) {
  modalCancel.addEventListener('click', () => {
    if (modalLocked) return;
    closeModal(false);
  });
}

if (modalConfirm) {
  modalConfirm.addEventListener('click', () => {
    if (modalLocked) return;
    closeModal(true);
  });
}

if (modalOverlay) {
  modalOverlay.addEventListener('click', (event) => {
    if (event.target === modalOverlay && !modalLocked) {
      closeModal(false);
    }
  });
}

if (stepBack) {
  stepBack.addEventListener('click', () => {
    const objective = currentObjective();
    if (!objective) return;
    const previous = previousFocusMode(objective);
    state.manualFocusMode = previous;
    state.showInlineReview = false;
    setExpandAll(false);
    applyFocusMode(objective);
  });
}

if (stepExpand) {
  stepExpand.addEventListener('click', () => {
    setExpandAll(!state.expandAll);
    if (state.expandAll) {
      state.manualFocusMode = null;
    }
    applyFocusMode(currentObjective());
  });
}

if (nextActionSaved) {
  nextActionSaved.addEventListener('click', (event) => {
    const saved = event.target.closest('[data-step]');
    if (!saved) return;
    state.manualFocusMode = saved.dataset.step || null;
    setExpandAll(false);
    applyFocusMode(currentObjective());
  });
}

if (atomicList) {
  atomicList.addEventListener('click', async (event) => {
    // Retry single failed task
    const retryBtn = event.target.closest('[data-retry-task]');
    if (retryBtn) {
      event.stopPropagation();
      if (retryBtn.disabled) return;
      const taskId = retryBtn.dataset.retryTask;
      const previousLabel = retryBtn.textContent;
      retryBtn.disabled = true;
      retryBtn.textContent = 'Retrying...';
      try {
        await api(`/api/tasks/${encodeURIComponent(taskId)}/retry`, { method: 'POST' });
        await loadWorkspace();
      } catch (e) {
        retryBtn.disabled = false;
        retryBtn.textContent = previousLabel;
        showError(e.message || 'Retry failed');
      }
      return;
    }
    // Retry all failed tasks
    const retryAllBtn = event.target.closest('#retry-all-failed');
    if (retryAllBtn) {
      event.stopPropagation();
      if (retryAllBtn.disabled) return;
      const previousLabel = retryAllBtn.textContent;
      retryAllBtn.disabled = true;
      retryAllBtn.textContent = 'Retrying...';
      try {
        await api(`/api/projects/${encodeURIComponent(state.projectId)}/retry-failed`, { method: 'POST' });
        await loadWorkspace();
      } catch (e) {
        retryAllBtn.disabled = false;
        retryAllBtn.textContent = previousLabel;
        showError(e.message || 'Retry all failed');
      }
      return;
    }
    // Card selection
    const card = event.target.closest('[data-atomic-task]');
    if (!card) return;
    state.taskId = card.dataset.atomicTask || null;
    const task = (state.workspace?.tasks || []).find((item) => item.id === state.taskId);
    state.runId = task ? (([...(task.runs || [])].reverse()[0] || {}).id || null) : null;
    renderAtomicUnits();
    renderRuns();
    loadRunOutput();
  });
}

const supervisorStartBtn = document.getElementById('supervisor-start-btn');
const supervisorStopBtn = document.getElementById('supervisor-stop-btn');

if (supervisorStartBtn) {
  supervisorStartBtn.addEventListener('click', async () => {
    if (!state.projectId) return;
    try {
      clearError();
      await api(`/api/projects/${encodeURIComponent(state.projectId)}/supervise`, { method: 'POST' });
      await loadWorkspace();
    } catch (error) {
      showError(error.message || 'Unable to start harness');
    }
  });
}

if (supervisorStopBtn) {
  supervisorStopBtn.addEventListener('click', async () => {
    if (!state.projectId) return;
    try {
      clearError();
      supervisorStopBtn.textContent = 'Stopping...';
      supervisorStopBtn.disabled = true;
      await api(`/api/projects/${encodeURIComponent(state.projectId)}/supervise/stop`, { method: 'POST' });
      await loadWorkspace();
    } catch (error) {
      showError(error.message || 'Unable to stop harness');
    } finally {
      supervisorStopBtn.textContent = 'Stop harness';
      supervisorStopBtn.disabled = false;
    }
  });
}

function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

// === Harness dashboard ===

let harnessData = null;

async function loadHarnessDashboard() {
  try {
    harnessData = await api('/api/harness');
    renderHarnessDashboard();
  } catch (_error) {
    // Silently retry on next poll.
  }
}

function renderHarnessDashboard() {
  if (!harnessData) return;
  const globalStatus = document.getElementById('harness-global-status');
  const llmHealth = document.getElementById('harness-llm-health');
  const projectCards = document.getElementById('harness-project-cards');
  const feedList = document.getElementById('harness-feed-list');
  if (!globalStatus) return;

  const gc = harnessData.global_counts || {};
  const gt = harnessData.global_total || 0;
  const pct = (n) => gt > 0 ? ((n / gt) * 100).toFixed(1) + '%' : '0%';
  const anyRunning = (harnessData.projects || []).some((p) => p.supervisor?.running);
  const statusWord = anyRunning ? 'Working' : 'Idle';

  globalStatus.innerHTML = `
    <h2>Harness — ${escapeHtml(statusWord)}</h2>
    <div class="global-progress-bar">
      <div class="segment completed" style="width:${pct(gc.completed || 0)}"></div>
      <div class="segment active" style="width:${pct(gc.active || 0)}"></div>
      <div class="segment failed" style="width:${pct(gc.failed || 0)}"></div>
      <div class="segment pending" style="width:${pct(gc.pending || 0)}"></div>
    </div>
    <div class="summary">
      ${gc.completed || 0} done · ${gc.active || 0} active · ${gc.failed || 0} failed · ${gc.pending || 0} pending — ${gt} total tasks
    </div>
  `;

  // LLM health badges
  const backends = harnessData.llm_health || [];
  llmHealth.innerHTML = backends.length
    ? backends.map((b) => `
        <span class="llm-badge ${b.demoted ? 'demoted' : 'healthy'}">
          <span class="dot"></span>
          ${escapeHtml(b.name)}${b.demoted ? ' (down)' : ''}
        </span>
      `).join('')
    : '<span style="color:var(--muted);font-size:0.85rem">No LLM backends configured</span>';

  // Project cards
  const projects = harnessData.projects || [];
  const statusOrder = { running: 0, starting: 1, error: 2, finished: 3, idle: 4 };
  const sorted = [...projects].sort((a, b) => {
    const aState = a.supervisor?.running ? 'running' : (a.supervisor?.state || 'idle');
    const bState = b.supervisor?.running ? 'running' : (b.supervisor?.state || 'idle');
    return (statusOrder[aState] ?? 5) - (statusOrder[bState] ?? 5);
  });
  projectCards.innerHTML = sorted.map((p) => {
    const ts = p.tasks_by_status || {};
    const total = p.task_total || 0;
    const ppct = (n) => total > 0 ? ((n / total) * 100).toFixed(1) + '%' : '0%';
    const supState = p.supervisor?.state || 'idle';
    const supRunning = p.supervisor?.running;
    const supClass = supRunning ? 'running' : supState === 'finished' ? 'finished' : supState === 'error' ? 'error' : 'idle';
    const obj = p.active_objective;
    const objLine = obj
      ? `<div class="objective-name">${escapeHtml(obj.title)} · ${escapeHtml(obj.status)}${obj.task_total ? ` · ${obj.task_counts?.completed || 0}/${obj.task_total} done` : ''}</div>`
      : '<div class="objective-name" style="color:var(--muted)">No active objective</div>';
    const atomicParams = new URLSearchParams({ project_id: p.id });
    if (obj) atomicParams.set('objective_id', obj.id);
    const atomicLink = `/atomic?${atomicParams.toString()}`;
    return `
      <a href="${atomicLink}" class="harness-project-card" style="text-decoration:none;color:inherit">
        <div class="project-header">
          <span class="project-name">${escapeHtml(p.name)}</span>
          <span class="supervisor-pill ${supClass}">${escapeHtml(supRunning ? 'running' : supState)}</span>
        </div>
        <div class="mini-progress">
          <div class="segment completed" style="width:${ppct(ts.completed || 0)}"></div>
          <div class="segment active" style="width:${ppct(ts.active || 0)}"></div>
          <div class="segment failed" style="width:${ppct(ts.failed || 0)}"></div>
          <div class="segment pending" style="width:${ppct(ts.pending || 0)}"></div>
        </div>
        <div class="task-summary">${ts.completed || 0} done · ${ts.active || 0} active · ${ts.failed || 0} failed · ${ts.pending || 0} pending — ${total} total</div>
        ${objLine}
      </a>
    `;
  }).join('');

  // Event feed
  const events = harnessData.recent_events || [];
  feedList.innerHTML = events.length
    ? events.map((e) => `
        <div class="harness-feed-item">
          <span class="feed-time">${escapeHtml(formatRelativeTime(e.created_at))}</span>
          <span class="feed-project">${escapeHtml(e.project_name)}</span>
          <span class="feed-text">${escapeHtml(e.text)}</span>
        </div>
      `).join('')
    : '<div style="color:var(--muted);padding:0.5rem">No recent events.</div>';
}

// === Main ===

async function main() {
  try {
    const ver = await api('/api/version');
    const tag = document.getElementById('version-tag');
    if (tag && ver.commit) tag.textContent = ver.commit;
  } catch (_e) {}
  if (state.view === 'harness') {
    await loadHarnessDashboard();
    window.setInterval(loadHarnessDashboard, 3000);
    return;
  }
  try {
    applySidebarState();
    await loadProjects();
    await loadWorkspace();
    // Tick active-timer runtime labels every second without rebuilding the DOM.
    window.setInterval(() => {
      document.querySelectorAll('.runtime.active-timer').forEach((el) => {
        const started = el.dataset.started;
        if (!started) return;
        const secs = Math.max(0, Math.floor((Date.now() - new Date(started).getTime()) / 1000));
        let text;
        if (secs < 60) text = secs + 's';
        else { const m = Math.floor(secs/60), r = secs%60; text = m < 60 ? m+'m '+r+'s' : Math.floor(m/60)+'h '+(m%60)+'m'; }
        el.textContent = text;
      });
      document.querySelectorAll('.last-activity-pill[data-timestamp]').forEach((el) => {
        const ts = el.dataset.timestamp;
        if (!ts) return;
        const roundTag = el.textContent.includes('(round') ? el.textContent.slice(el.textContent.indexOf('(round')) : '';
        el.textContent = 'Last activity ' + formatRelativeTime(ts) + (roundTag ? ' ' + roundTag : '');
      });
      renderMermaidMeta(currentObjective());
    }, 1000);
    // Use SSE for data updates; fall back to polling if EventSource unavailable.
    if (typeof EventSource !== 'undefined') {
      let es = new EventSource('/api/events');
      es.onmessage = async (evt) => {
        if (evt.data === 'workspace-changed') {
          try { await loadWorkspace(); } catch (_e) {}
        }
      };
      es.onerror = () => {
        // Reconnect is automatic with EventSource, but refresh data on recovery.
        setTimeout(async () => {
          try { await loadWorkspace(); } catch (_e) {}
        }, 3000);
      };
    } else {
      window.setInterval(async () => {
        try {
          const objective = currentObjective();
          const supervisorActive = state.workspace?.supervisor?.running || state.workspace?.supervisor?.state === 'running';
          if (state.view === 'atomic' && (objective?.atomic_generation?.status === 'running' || supervisorActive)) {
            await loadWorkspace();
          }
        } catch (_error) {}
      }, 5000);
    }
  } catch (error) {
    showError(error.message || 'Failed to load workspace');
  }
}

main();
"""


_FULL_UI_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Accruvia Harness</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="/app.css">
  </head>
  <body data-view="default" data-layout="standard">
    <div id="app-shell" class="app-shell">
      <aside class="sidebar">
        <button id="sidebar-toggle" class="sidebar-toggle" type="button">
          <span id="sidebar-toggle-label"><</span>
        </button>
        <div class="sidebar-body">
        <h1>Harness UI</h1>
        <p class="subtle">Real runs, rendered control flow, and operator-only notes.</p>
        <label for="project-select">Project</label>
        <select id="project-select" class="selector"></select>
        <div class="list">
          <div>
            <div class="section-title">Objectives</div>
            <div id="objective-list" class="list"></div>
            <form id="create-objective-form">
              <div class="form-row">
                <input id="create-objective-title" type="text" placeholder="New objective title">
                <textarea id="create-objective-summary" placeholder="Optional summary"></textarea>
              </div>
              <div class="actions">
                <button type="submit">Create objective</button>
              </div>
            </form>
          </div>
          <div>
            <div class="title">Tasks</div>
            <div id="task-list" class="list"></div>
          </div>
          <div>
            <div class="title">Runs</div>
            <div id="run-list" class="list"></div>
          </div>
        </div>
        </div>
      </aside>
      <main class="content">
        <div id="page-error" class="error" hidden></div>
        <div class="header">
          <div>
            <h2 id="workspace-title">Harness project</h2>
            <p id="workspace-summary"></p>
            <nav id="view-nav" class="view-nav"></nav>
            <div class="header-actions">
              <button id="header-create-objective" class="header-button" type="button">New Objective</button>
            </div>
          </div>
          <div id="workspace-status" class="status-chip">idle</div>
        </div>
        <section id="next-action-panel" class="panel">
          <div id="objective-banner" class="execution-summary" hidden>
            <div>
              <div class="label">Current objective</div>
              <div id="objective-banner-title" class="value">No objective selected</div>
            </div>
            <div>
              <div class="label">Mode</div>
              <div id="objective-banner-meta" class="body"></div>
            </div>
            <div id="version-tag" style="font-size:0.7rem;color:var(--muted);margin-left:auto;white-space:nowrap;"></div>
            <div class="atomic-objective-picker">
              <div class="label">Project</div>
              <select id="banner-project-select"></select>
            </div>
            <div class="atomic-objective-picker">
              <div class="label">Select objective</div>
              <select id="atomic-objective-select"></select>
            </div>
          </div>
          <h3 id="next-action-title">No objective selected</h3>
          <p id="next-action-body" class="hint">Create or select an objective to get a guided next step.</p>
          <div class="expectation-grid">
            <div class="expectation-card">
              <div class="label">Your role</div>
              <div id="expectation-role" class="value">You are the Operator</div>
            </div>
            <div class="expectation-card">
              <div class="label">What I need from you</div>
              <div id="expectation-need" class="value">Select or create one objective.</div>
            </div>
            <div class="expectation-card">
              <div class="label">Why</div>
              <div id="expectation-why" class="value">The harness needs one active target.</div>
            </div>
            <div class="expectation-card">
              <div class="label">Done when</div>
              <div id="expectation-done" class="value">Done when one objective is selected.</div>
            </div>
          </div>
          <div id="next-action-saved"></div>
          <div class="conversation-label">Harness output</div>
          <div id="conversation-transcript" class="conversation-transcript"></div>
          <form id="conversation-form" class="conversation-form" hidden>
          <div class="conversation-label">Type to the harness here</div>
          <div id="diagram-comment-anchor" class="diagram-comment-anchor">
            <div>
              <div class="meta">Diagram comment target</div>
              <div id="diagram-comment-anchor-label" class="value"></div>
            </div>
            <button id="diagram-comment-anchor-clear" type="button">Clear</button>
          </div>
          <textarea id="conversation-input" class="conversation-textarea" placeholder="Ask the harness a question, answer the prompt, or clarify the objective here."></textarea>
          <div class="actions">
              <button id="conversation-submit" type="submit">Send to harness</button>
              <button id="conversation-interrupt" type="button" hidden>Interrupt</button>
          </div>
          </form>
          <div id="proposal-actions" class="actions" hidden></div>
          <div id="conversation-primary-actions" class="conversation-primary-actions actions" hidden>
            <button id="conversation-primary-button" type="button">Start this implementation step</button>
          </div>
          <section id="inline-output-panel" class="inline-output-panel" hidden>
            <div class="conversation-label">Latest run review</div>
            <div id="inline-output-summary" class="execution-summary" hidden>
              <div id="inline-output-summary-body"></div>
            </div>
            <div class="actions">
              <button id="inline-output-toggle" type="button">Show raw evidence</button>
            </div>
            <div id="inline-output-tabs" class="output-tabs" hidden></div>
            <div id="inline-output-body" class="output-body" hidden></div>
          </section>
          <div class="step-actions">
            <button id="step-back" type="button">Back</button>
            <button id="step-expand" type="button">Show everything</button>
          </div>
        </section>
        <div id="content-grid" class="grid">
          <section id="new-objective-panel" class="panel" hidden>
            <div class="objective-create-shell">
              <div class="objective-create-hero">
                <h3>New objective</h3>
                <p>Create a new durable workstream instead of filling a popup intake. You can add intent and tighten the control flow immediately after creation.</p>
              </div>
              <form id="page-create-objective-form" class="objective-create-form">
                <div class="field-grid">
                  <label>
                    Project
                    <select id="page-create-objective-project"></select>
                  </label>
                  <label>
                    Objective title
                    <input id="page-create-objective-title" type="text" placeholder="Context Management">
                  </label>
                  <label>
                    Summary
                    <textarea id="page-create-objective-summary" placeholder="Centralize context assembly, keep context rich for now, and add observability so later trimming is evidence-based."></textarea>
                  </label>
                </div>
                <div class="objective-create-actions">
                  <button type="submit">Create objective</button>
                  <a class="objective-create-cancel" href="/workspace">Cancel</a>
                </div>
              </form>
            </div>
          </section>
          <section id="token-performance-panel" class="panel" hidden>
            <div id="token-performance-content"></div>
          </section>
          <section id="settings-panel" class="panel" hidden>
            <div id="settings-content"></div>
          </section>
          <section id="objective-panel" class="panel">
            <h3 id="objective-title">Current objective</h3>
            <p id="objective-summary" class="hint">No objective selected.</p>
            <form id="intent-form">
              <div id="step-prompt" class="step-prompt" hidden>
                <div id="step-question" class="question"></div>
                <div id="step-helper" class="helper"></div>
              </div>
              <div class="form-row">
                <textarea id="intent-summary" placeholder="Desired outcome: what do you want the system to achieve for this objective?"></textarea>
                <textarea id="success-definition" placeholder="Success definition: how will you know this objective is actually done?"></textarea>
                <textarea id="non-negotiables" placeholder="Non-negotiables: constraints or solution-shape requirements, one per line"></textarea>
                <textarea id="frustration-signals" placeholder="Frustration signals: what would tell us reality is drifting from your intent, one per line"></textarea>
              </div>
              <div class="actions">
                <button id="intent-save-button" type="submit">Save intent</button>
              </div>
            </form>
            <div id="objective-gate-section" hidden>
              <div class="section-title">Execution gates</div>
              <div id="objective-gate" class="comment-list"></div>
            </div>
          </section>
          <section id="interrogation-panel" class="panel" hidden>
            <h3>Interrogation Review</h3>
            <p id="interrogation-summary" class="hint"></p>
            <div class="execution-summary">
              <div>
                <div class="label">Current plan elements</div>
                <ul id="interrogation-plan" class="summary-list"></ul>
              </div>
              <div>
                <div class="label">Red-team questions</div>
                <ul id="interrogation-questions" class="summary-list"></ul>
              </div>
            </div>
            <div class="actions">
              <button id="interrogation-complete-button" type="button">Finish interrogation</button>
            </div>
          </section>
          <section id="mermaid-panel" class="panel">
            <h3>Control Logic</h3>
            <p id="mermaid-meta" class="hint">No Mermaid artifact yet.</p>
            <div id="mermaid-step-prompt" class="step-prompt" hidden>
              <div id="mermaid-step-question" class="question"></div>
              <div id="mermaid-step-helper" class="helper"></div>
            </div>
            <div id="mermaid-proposal-summary" class="step-prompt" hidden></div>
            <div id="diagram-shell" class="diagram-shell"></div>
            <div id="mermaid-controls" class="actions">
              <button type="button" data-mermaid-action="finished">Matches my flow</button>
              <button type="button" data-mermaid-action="paused">Doesn't match yet</button>
            </div>
          </section>
          <section id="execution-panel" class="panel" hidden>
            <h3>The Only Thing To Do Next</h3>
            <p class="hint">This is the single bounded step the harness wants to run right now.</p>
            <div class="execution-summary">
              <div>
                <div class="label">Bounded slice</div>
                <div id="execution-title" class="value">No bounded slice yet</div>
              </div>
              <div>
                <div class="label">Why it exists</div>
                <div id="execution-objective" class="body"></div>
              </div>
              <div>
                <div class="label">Task</div>
                <div id="execution-task-meta"></div>
              </div>
              <div>
                <div class="label">Run</div>
                <div id="execution-run-meta"></div>
              </div>
              <div>
                <div class="label">What will happen next</div>
                <div id="execution-explanation" class="body"></div>
              </div>
            </div>
            <div class="actions">
              <button id="execution-primary-button" type="button">Start first slice now</button>
            </div>
          </section>
          <section id="supervisor-panel" class="panel">
            <h3>Harness Execution</h3>
            <p id="supervisor-status" class="hint">Harness is idle.</p>
            <div id="supervisor-meta" class="atomic-generation-meta"></div>
            <div class="actions" style="margin-top:0.5rem">
              <button id="supervisor-start-btn" type="button">Start harness</button>
              <button id="supervisor-stop-btn" type="button" hidden>Stop harness</button>
            </div>
          </section>
          <section id="atomic-panel" class="panel" hidden>
            <h3 id="atomic-title">Atomic units of work</h3>
            <p id="atomic-summary" class="hint"></p>
            <p id="atomic-generation-status" class="hint"></p>
            <div id="atomic-generation-meta" class="atomic-generation-meta"></div>
            <div id="atomic-list" class="atomic-list"></div>
          </section>
          <section id="promotion-review-panel" class="panel" hidden>
            <h3 id="promotion-review-title">Promotion review</h3>
            <p id="promotion-review-summary" class="hint">Select an objective to inspect promotion readiness and recorded reviews.</p>
            <div id="promotion-review-meta" class="atomic-generation-meta"></div>
            <div id="promotion-review-content" class="promotion-grid"></div>
          </section>
          <section id="cli-panel" class="panel">
            <h3>CLI Output</h3>
            <p class="hint">Readable text artifacts from the selected run directory.</p>
            <div id="output-tabs" class="output-tabs"></div>
            <div id="output-body" class="output-body"></div>
          </section>
          <section id="harness-dashboard" class="panel" hidden>
            <div id="harness-global-status" class="harness-global-status"></div>
            <div id="harness-llm-health" class="harness-llm-health"></div>
            <div id="harness-project-cards" class="harness-project-cards"></div>
            <div id="harness-event-feed" class="harness-event-feed">
              <h3>Live feed</h3>
              <div id="harness-feed-list" class="harness-feed-list"></div>
            </div>
          </section>
        </div>
        <section id="promotion-review-rounds-panel" class="panel" hidden>
          <div id="promotion-review-rounds-content" class="promotion-grid"></div>
        </section>
      </main>
    </div>
    <div id="modal-overlay" class="modal-overlay" hidden>
      <div id="modal-card" class="modal-card" role="dialog" aria-modal="true" aria-labelledby="modal-title">
        <h3 id="modal-title" class="modal-title"></h3>
        <div id="modal-body" class="modal-body"></div>
        <div id="modal-status-row" class="modal-status-row" hidden>
          <div class="modal-spinner" aria-hidden="true"></div>
          <div id="modal-status-text" class="modal-status-text"></div>
        </div>
        <div class="modal-actions">
          <button id="modal-cancel" type="button" class="secondary">Cancel</button>
          <button id="modal-confirm" type="button">OK</button>
        </div>
      </div>
    </div>
    <script type="module" src="/app.js"></script>
  </body>
</html>
"""

def _render_view_html(view: str, layout: str) -> str:
    html = _FULL_UI_HTML.replace('data-view="default"', f'data-view="{view}"', 1)
    return html.replace('data-layout="standard"', f'data-layout="{layout}"', 1)


_INDEX_HTML = _render_view_html("control-flow", "split-workspace")
_ATOMIC_HTML = _render_view_html("atomic", "split-workspace")
_PROMOTION_REVIEW_HTML = _render_view_html("promotion-review", "split-workspace")
_OBJECTIVE_CREATE_HTML = _render_view_html("objective-create", "full-review")
_SETTINGS_HTML = _render_view_html("settings", "full-review")
_TOKEN_PERFORMANCE_HTML = _render_view_html("token-performance", "full-review")
_HARNESS_HTML = _render_view_html("harness", "dashboard")


@dataclass(slots=True)
class RunOutputSection:
    label: str
    path: str
    content: str


class HarnessUIDataService:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.store = ctx.store
        self.query_service = ctx.query_service
        self.workspace_root = ctx.config.workspace_root
        self.task_service = TaskService(self.store)
        self.memory_provider = LocalContextMemoryProvider(self.store)
        self.auto_resume_atomic_generation = not bool(getattr(ctx, "is_test", False))
        self.auto_resume_objective_review = not bool(getattr(ctx, "is_test", False))

    def list_projects(self) -> dict[str, object]:
        projects = []
        for project in self.store.list_projects():
            metrics = self.store.metrics_snapshot(project.id)
            projects.append(
                {
                    **serialize_dataclass(project),
                    "queue_depth": int(metrics.get("tasks_by_status", {}).get("pending", 0))
                    + int(metrics.get("tasks_by_status", {}).get("active", 0)),
                }
            )
        return {"projects": projects}

    def update_project_repo_settings(
        self,
        project_id: str,
        *,
        promotion_mode: str,
        repo_provider: str,
        repo_name: str,
        base_branch: str,
    ) -> dict[str, object]:
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        cleaned_repo_name = repo_name.strip()
        cleaned_base_branch = base_branch.strip()
        if not cleaned_repo_name:
            raise ValueError("Repository name must not be empty")
        if not cleaned_base_branch:
            raise ValueError("Base branch must not be empty")
        updated = self.task_service.update_project(
            project.id,
            promotion_mode=PromotionMode(promotion_mode),
            repo_provider=RepoProvider(repo_provider),
            repo_name=cleaned_repo_name,
            base_branch=cleaned_base_branch,
        )
        return {"project": serialize_dataclass(updated)}

    def promote_objective_to_repo(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        project = self.store.get_project(objective.project_id)
        if project is None:
            raise ValueError(f"Unknown project for objective: {objective.project_id}")
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
        review = self._promotion_review_for_objective(objective.id, linked_tasks)
        override_active = bool(review.get("operator_override"))
        if not bool(review.get("review_clear")) and not override_active:
            raise ValueError("Objective is not yet clear to promote")
        objective_paths = self._objective_repo_file_set(linked_tasks)
        if not objective_paths:
            raise ValueError("Objective promotion could not determine any objective-related file paths to apply")
        source_repo_root = self._objective_source_repo_root(objective.id, linked_tasks)
        if source_repo_root is None:
            raise ValueError("Objective promotion requires a git-backed source repository root")
        candidate = self._latest_completed_task_for_objective(linked_tasks)
        candidate_run_id = ""
        if candidate is not None:
            runs = self.store.list_runs(candidate.id)
            completed_run = next((run for run in reversed(runs) if run.status == RunStatus.COMPLETED), None)
            candidate_run_id = completed_run.id if completed_run is not None else ""
        apply_result = self.ctx.engine.repository_promotions.apply_objective(
            project,
            objective_id=objective.id,
            objective_title=objective.title,
            source_repo_root=source_repo_root,
            source_working_root=source_repo_root,
            objective_paths=objective_paths,
            staging_root=self.workspace_root / "objective_promotions",
        )
        applyback = {
            "status": "applied",
            "branch_name": apply_result.branch_name,
            "commit_sha": apply_result.commit_sha,
            "pushed_ref": apply_result.pushed_ref,
            "pr_url": apply_result.pr_url,
            "promotion_mode": project.promotion_mode.value,
            "cleanup_performed": apply_result.cleanup_performed,
            "verified_remote_sha": apply_result.verified_remote_sha,
            "objective_paths": objective_paths,
            "source_repo_root": str(source_repo_root),
        }
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                task_id=candidate.id if candidate is not None else None,
                run_id=candidate_run_id or None,
                visibility="operator_visible",
                author_type="system",
                content="Action receipt: Promoted the objective snapshot to the repository.",
                metadata={
                    "kind": "objective_repo_promotion",
                    "task_id": candidate.id if candidate is not None else "",
                    "run_id": candidate_run_id,
                    "promotion_status": "approved",
                    "applyback": applyback,
                    "objective_paths": objective_paths,
                },
            )
        )
        return {
            "objective_id": objective.id,
            "task_id": candidate.id if candidate is not None else "",
            "run_id": candidate_run_id,
            "promotion": {
                "id": new_id("promotion"),
                "task_id": candidate.id if candidate is not None else "",
                "run_id": candidate_run_id,
                "status": "approved",
                "summary": "Objective snapshot promoted to the repository.",
                "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
            },
            "applyback": applyback,
        }

    def project_workspace(self, project_ref: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objectives = self.store.list_objectives(project.id)
        if self.auto_resume_atomic_generation:
            for objective in objectives:
                self._maybe_resume_atomic_generation(objective.id)
        if self.auto_resume_objective_review:
            for objective in objectives:
                self._maybe_resume_objective_review(objective.id)
        objectives = self.store.list_objectives(project.id)
        tasks = self.store.list_tasks(project.id)
        task_payload = []
        latest_runs_by_task: dict[str, list[Any]] = {}
        for task in tasks:
            runs = self.store.list_runs(task.id)
            promotions = self.store.list_promotions(task.id)
            latest_runs_by_task[task.id] = runs
            task_payload.append(
                {
                    **serialize_dataclass(task),
                    "runs": [serialize_dataclass(run) for run in runs],
                    "promotions": [serialize_dataclass(promotion) for promotion in promotions],
                }
            )
        objective_payload = []
        for objective in objectives:
            latest_intent = self.store.latest_intent_model(objective.id)
            latest_mermaid = self.store.latest_mermaid_artifact(objective.id)
            latest_proposal = self._latest_mermaid_proposal(objective.id)
            gate = objective_execution_gate(self.store, objective.id)
            linked_tasks = [task for task in tasks if task.objective_id == objective.id]
            atomic_generation = self._atomic_generation_state(objective.id)
            objective_payload.append(
                {
                    **serialize_dataclass(objective),
                    "execution_gate": {
                        "ready": gate.ready,
                        "checks": gate.gate_checks,
                    },
                    "intent_model": serialize_dataclass(latest_intent) if latest_intent is not None else None,
                    "interrogation_review": self._interrogation_review(objective.id),
                    "diagram": (
                        {
                            **serialize_dataclass(latest_mermaid),
                            "content": latest_mermaid.content,
                        }
                        if latest_mermaid is not None
                        else None
                    ),
                    "diagram_proposal": latest_proposal,
                    "linked_task_count": len(linked_tasks),
                    "atomic_generation": atomic_generation,
                    "atomic_units": self._atomic_units_for_objective(objective.id, linked_tasks, atomic_generation),
                    "promotion_review": self._promotion_review_for_objective(objective.id, linked_tasks),
                    "repo_promotion": self._repo_promotion_for_objective(objective.id, linked_tasks),
                    "recommended_view": (
                        "promotion-review"
                        if objective.status == ObjectiveStatus.RESOLVED
                        else "atomic"
                    ),
                    "proposed_first_task": self.proposed_first_task(objective.id)
                    if gate.ready and not linked_tasks
                    else None,
                }
            )
        return {
            "project": serialize_dataclass(project),
            "objectives": objective_payload,
            "tasks": task_payload,
            "comments": self._operator_comments(project.id),
            "replies": self._harness_replies(project.id),
            "action_receipts": self._action_receipts(project.id),
            "frustrations": self._operator_frustrations(project.id),
            "loop_status": self.query_service.project_summary(project.id)["loop_status"],
            "diagram": {
                "label": "Project control flow",
                "mermaid": self._project_mermaid(project.id, tasks, latest_runs_by_task),
            },
            "supervisor": {
                "running": _BACKGROUND_SUPERVISOR.is_running(project.id),
                **_BACKGROUND_SUPERVISOR.status(project.id),
            },
        }

    def _latest_completed_task_for_objective(self, linked_tasks: list[Task]) -> Task | None:
        best: tuple[str, str, str] | None = None
        selected: Task | None = None
        for task in linked_tasks:
            if task.status != TaskStatus.COMPLETED:
                continue
            runs = self.store.list_runs(task.id)
            completed_run = next((run for run in reversed(runs) if run.status == RunStatus.COMPLETED), None)
            if completed_run is None:
                continue
            score = (
                str(completed_run.created_at or ""),
                str(task.created_at or ""),
                task.id,
            )
            if best is None or score > best:
                best = score
                selected = task
        return selected

    def _objective_repo_file_set(self, linked_tasks: list[Task]) -> list[str]:
        file_paths: set[str] = set()
        for task in linked_tasks:
            runs = self.store.list_runs(task.id)
            for run in runs:
                report_artifacts = [artifact for artifact in self.store.list_artifacts(run.id) if artifact.kind == "report" and artifact.path]
                if not report_artifacts:
                    continue
                report_path = Path(report_artifacts[-1].path)
                try:
                    payload = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                changed_files = payload.get("changed_files")
                if isinstance(changed_files, list):
                    for raw_path in changed_files:
                        path = str(raw_path or "").strip()
                        if path and not path.startswith("/") and ".." not in Path(path).parts:
                            file_paths.add(str(Path(path)))
        return sorted(file_paths)

    def _objective_source_repo_root(self, objective_id: str, linked_tasks: list[Task]) -> Path | None:
        for task in reversed(linked_tasks):
            runs = self.store.list_runs(task.id)
            for run in reversed(runs):
                events = self.store.list_events(entity_type="run", entity_id=run.id)
                for event in reversed(events):
                    if event.event_type != "project_workspace_prepared":
                        continue
                    source_repo_root = str(event.payload.get("source_repo_root") or "").strip()
                    if source_repo_root:
                        return Path(source_repo_root).resolve()
        return None

    def _latest_objective_repo_promotion(self, objective_id: str) -> dict[str, object] | None:
        records = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="action_receipt")
            if str(record.metadata.get("kind") or "") == "objective_repo_promotion"
        ]
        if not records:
            return None
        record = records[-1]
        applyback = dict(record.metadata.get("applyback") or {})
        return {
            "id": record.id,
            "status": "approved",
            "summary": record.content,
            "created_at": record.created_at.isoformat(),
            "applyback": applyback,
            "task_id": str(record.metadata.get("task_id") or ""),
            "run_id": str(record.metadata.get("run_id") or ""),
        }

    def _missing_repo_promotion_validation_reason(self, run_id: str) -> str:
        report_artifacts = [artifact for artifact in self.store.list_artifacts(run_id) if artifact.kind == "report" and artifact.path]
        if not report_artifacts:
            return "The latest completed run does not have a structured report artifact."
        report_path = Path(report_artifacts[-1].path)
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "The latest completed run has an unreadable structured report artifact."
        compile_check = payload.get("compile_check")
        test_check = payload.get("test_check")
        if isinstance(compile_check, dict) and isinstance(test_check, dict):
            return ""
        return (
            "The latest completed run is missing persisted compile/test validation evidence in report.json. "
            "Re-run or re-validate the task before repo promotion."
        )

    def _repo_promotion_for_objective(self, objective_id: str, linked_tasks: list[Task]) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        project = self.store.get_project(objective.project_id)
        if project is None:
            raise ValueError(f"Unknown project for objective: {objective.project_id}")
        review = self._promotion_review_for_objective(objective.id, linked_tasks)
        override_active = bool(review.get("operator_override"))
        candidate = self._latest_completed_task_for_objective(linked_tasks)
        candidate_payload: dict[str, object] | None = None
        latest_promotion_payload: dict[str, object] | None = self._latest_objective_repo_promotion(objective.id)
        reason = ""
        eligible = False
        objective_paths = self._objective_repo_file_set(linked_tasks)
        source_repo_root = self._objective_source_repo_root(objective.id, linked_tasks)

        if candidate is None:
            reason = "No completed linked task is available yet."
        else:
            runs = self.store.list_runs(candidate.id)
            completed_run = next((run for run in reversed(runs) if run.status == RunStatus.COMPLETED), None)
            candidate_payload = {
                "task_id": candidate.id,
                "title": candidate.title,
                "status": candidate.status.value,
                "latest_completed_run_id": completed_run.id if completed_run is not None else "",
                "latest_completed_attempt": completed_run.attempt if completed_run is not None else None,
            }
            if completed_run is None:
                reason = "The latest completed linked task does not have a completed run."
            elif not objective_paths:
                reason = "Objective promotion could not determine any objective-related file paths to apply."
            elif source_repo_root is None:
                reason = "Objective promotion requires a git-backed source repository root."
            else:
                if not bool(review.get("review_clear")) and not override_active:
                    reason = "Objective review must be clear before repo promotion."
                else:
                    eligible = True
                    reason = (
                        f"Operator override is active. Repo promotion will stage the current objective snapshot for {len(objective_paths)} tracked file(s) and apply it to the repository."
                        if override_active and not bool(review.get("review_clear"))
                        else f"The objective snapshot is ready to promote to the repository with {len(objective_paths)} tracked file(s)."
                    )

        return {
            "eligible": eligible,
            "reason": reason,
            "project_settings": {
                "promotion_mode": project.promotion_mode.value,
                "repo_provider": project.repo_provider.value if project.repo_provider is not None else "",
                "repo_name": project.repo_name,
                "base_branch": project.base_branch,
            },
            "candidate": candidate_payload,
            "latest_promotion": latest_promotion_payload,
        }

    def create_objective(self, project_ref: str, title: str, summary: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ValueError("Objective title must not be empty")
        objective = Objective(
            id=new_id("objective"),
            project_id=project.id,
            title=cleaned_title,
            summary=summary.strip(),
        )
        self.store.create_objective(objective)
        self._create_seed_mermaid(objective)
        return {"objective": serialize_dataclass(objective)}

    def update_intent_model(
        self,
        objective_id: str,
        *,
        intent_summary: str,
        success_definition: str,
        non_negotiables: list[str],
        frustration_signals: list[str],
        author_type: str = "operator",
    ) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        summary = intent_summary.strip()
        if not summary:
            raise ValueError("Intent summary must not be empty")
        model = IntentModel(
            id=new_id("intent"),
            objective_id=objective.id,
            version=self.store.next_intent_model_version(objective.id),
            intent_summary=summary,
            success_definition=success_definition.strip(),
            non_negotiables=[item for item in (part.strip() for part in non_negotiables) if item],
            frustration_signals=[item for item in (part.strip() for part in frustration_signals) if item],
            author_type=author_type,
        )
        self.store.create_intent_model(model)
        return {"intent_model": serialize_dataclass(model)}

    def complete_interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        review = self._interrogation_review(objective_id)
        if review.get("completed"):
            return {"interrogation_review": review}
        if review.get("generated_by") == "deterministic":
            review = self._generate_interrogation_review(objective_id)
        self._persist_interrogation_record("interrogation_completed", objective, review)
        return {"interrogation_review": self._interrogation_review(objective.id)}

    def update_mermaid_artifact(
        self,
        objective_id: str,
        *,
        status: str,
        summary: str,
        blocking_reason: str,
        author_type: str = "operator",
        async_generation: bool = True,
    ) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        if getattr(self.ctx, "is_test", False):
            async_generation = False
        normalized = status.strip().lower()
        try:
            next_status = MermaidStatus(normalized)
        except ValueError as exc:
            raise ValueError(f"Unsupported Mermaid status: {status}") from exc

        latest = self.store.latest_mermaid_artifact(objective.id, "workflow_control")
        if latest is None:
            latest = self._create_seed_mermaid(objective)
        content = latest.content if latest is not None else self._default_objective_mermaid(objective)
        artifact = MermaidArtifact(
            id=new_id("diagram"),
            objective_id=objective.id,
            diagram_type="workflow_control",
            version=self.store.next_mermaid_version(objective.id, "workflow_control"),
            status=next_status,
            summary=(summary.strip() or latest.summary or f"{next_status.value} workflow review"),
            content=content,
            required_for_execution=True,
            blocking_reason=blocking_reason.strip(),
            author_type=author_type,
        )
        self.store.create_mermaid_artifact(artifact)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_status_change",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type=author_type,
                content=f"Mermaid workflow_control marked {next_status.value}",
                metadata={
                    "diagram_id": artifact.id,
                    "diagram_type": artifact.diagram_type,
                    "version": artifact.version,
                    "status": artifact.status.value,
                    "required_for_execution": artifact.required_for_execution,
                    "blocking_reason": artifact.blocking_reason,
                },
            )
        )
        if next_status == MermaidStatus.PAUSED:
            self.store.update_objective_status(objective.id, ObjectiveStatus.PAUSED)
        elif next_status == MermaidStatus.FINISHED:
            self.store.update_objective_status(objective.id, ObjectiveStatus.PLANNING)
            self.queue_atomic_generation(objective.id, async_mode=async_generation)
        else:
            self.store.update_objective_status(objective.id, ObjectiveStatus.INVESTIGATING)
        return {"diagram": serialize_dataclass(artifact)}

    def propose_mermaid_update(self, objective_id: str, *, directive: str) -> dict[str, object] | None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        proposal = self._generate_mermaid_update_proposal(objective_id, directive=directive)
        if proposal is None:
            return None
        record = ContextRecord(
            id=new_id("context"),
            record_type="mermaid_update_proposed",
            project_id=objective.project_id,
            objective_id=objective.id,
            visibility="model_visible",
            author_type="system",
            content=proposal["summary"],
            metadata={
                "content": proposal["content"],
                "summary": proposal["summary"],
                "directive": directive,
                "backend": proposal.get("backend", ""),
                "prompt_path": proposal.get("prompt_path", ""),
                "response_path": proposal.get("response_path", ""),
            },
        )
        self.store.create_context_record(record)
        return {
            "id": record.id,
            "summary": record.content,
            "content": str(record.metadata.get("content") or ""),
            "directive": directive,
            "created_at": record.created_at.isoformat(),
        }

    def accept_mermaid_proposal(self, objective_id: str, proposal_id: str, *, async_generation: bool = True) -> dict[str, object]:
        if getattr(self.ctx, "is_test", False):
            async_generation = False
        objective = self.store.get_objective(objective_id)
        proposal = self._proposal_record(objective_id, proposal_id)
        if objective is None or proposal is None:
            raise ValueError("Unknown Mermaid proposal")
        content = str(proposal.metadata.get("content") or "").strip()
        if not content:
            raise ValueError("Mermaid proposal content is empty")
        artifact = MermaidArtifact(
            id=new_id("diagram"),
            objective_id=objective.id,
            diagram_type="workflow_control",
            version=self.store.next_mermaid_version(objective.id, "workflow_control"),
            status=MermaidStatus.FINISHED,
            summary=str(proposal.metadata.get("summary") or proposal.content or "Accepted control flow"),
            content=content,
            required_for_execution=True,
            blocking_reason="",
            author_type="operator",
        )
        self.store.create_mermaid_artifact(artifact)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_status_change",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="operator",
                content="Mermaid workflow_control marked finished",
                metadata={
                    "diagram_id": artifact.id,
                    "diagram_type": artifact.diagram_type,
                    "version": artifact.version,
                    "status": artifact.status.value,
                    "required_for_execution": artifact.required_for_execution,
                    "blocking_reason": artifact.blocking_reason,
                },
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_update_accepted",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="operator",
                content="Accepted proposed Mermaid update.",
                metadata={"proposal_id": proposal.id, "diagram_id": artifact.id, "version": artifact.version},
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Action receipt: Exact proposal on screen promoted unchanged to locked current version {artifact.version}. No regeneration occurred.",
                metadata={
                    "kind": "mermaid_update",
                    "status": "accepted",
                    "proposal_id": proposal.id,
                    "diagram_id": artifact.id,
                    "promotion_mode": "exact_proposal",
                },
            )
        )
        self.store.update_objective_status(objective.id, ObjectiveStatus.PLANNING)
        self.queue_atomic_generation(objective.id, async_mode=async_generation)
        return {"diagram": serialize_dataclass(artifact)}

    def reject_mermaid_proposal(self, objective_id: str, proposal_id: str, *, resolution: str = "refine") -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        proposal = self._proposal_record(objective_id, proposal_id)
        if objective is None or proposal is None:
            raise ValueError("Unknown Mermaid proposal")
        normalized = resolution.strip().lower() or "refine"
        if normalized not in {"refine", "rewind_hard"}:
            raise ValueError(f"Unsupported Mermaid proposal resolution: {resolution}")
        record_type = "mermaid_update_rejected" if normalized == "refine" else "mermaid_update_rewound"
        content = "Keep refining the Mermaid update." if normalized == "refine" else "Rewind the Mermaid update and reconsider from the last approved diagram."
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type=record_type,
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="operator",
                content=content,
                metadata={"proposal_id": proposal.id, "resolution": normalized},
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=(
                    "Action receipt: Mermaid proposal kept for further refinement."
                    if normalized == "refine"
                    else "Action receipt: Mermaid proposal rewound hard to the last approved diagram."
                ),
                metadata={"kind": "mermaid_update", "status": normalized, "proposal_id": proposal.id},
            )
        )
        return {"rejected": True, "proposal_id": proposal.id, "resolution": normalized}

    def proposed_first_task(self, objective_id: str) -> dict[str, object]:
        linked_objective = self.store.get_objective(objective_id)
        if linked_objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        intent_model = self.store.latest_intent_model(objective_id)
        desired_outcome = (intent_model.intent_summary if intent_model is not None else "").strip()
        success_definition = (intent_model.success_definition if intent_model is not None else "").strip()
        summary = linked_objective.summary.strip()

        if desired_outcome:
            objective_text = desired_outcome
        elif summary:
            objective_text = summary
        else:
            objective_text = linked_objective.title

        if success_definition:
            objective_text = f"{objective_text} Success means: {success_definition}"

        return {
            "title": f"First slice: {linked_objective.title}",
            "objective": f"{objective_text} Keep the slice bounded and operator-visible.",
            "reason": "The harness generated this first slice from the objective, desired outcome, and success definition so you do not need to author the initial task manually.",
        }

    def create_linked_task(self, objective_id: str) -> dict[str, object]:
        linked_objective = self.store.get_objective(objective_id)
        if linked_objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        proposal = self.proposed_first_task(objective_id)
        task = self.task_service.create_task_with_policy(
            project_id=linked_objective.project_id,
            objective_id=linked_objective.id,
            title=str(proposal["title"]),
            objective=str(proposal["objective"]),
            priority=linked_objective.priority,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="generic",
            validation_mode="lightweight_operator",
            scope={},
            strategy="operator_ergonomics",
            max_attempts=3,
            required_artifacts=["plan", "report"],
        )
        self.store.update_objective_status(linked_objective.id, ObjectiveStatus.EXECUTING)
        return {"task": serialize_dataclass(task)}

    def queue_atomic_generation(self, objective_id: str, *, async_mode: bool = True) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        if mermaid is None or mermaid.status != MermaidStatus.FINISHED:
            raise ValueError("Atomic generation requires a finished Mermaid.")
        current = self._atomic_generation_state(objective_id)
        if current["status"] == "running" and self._atomic_generation_is_stale(current, objective_id):
            self._mark_atomic_generation_interrupted(objective, current)
            current = self._atomic_generation_state(objective_id)
        if current["status"] == "running" and int(current.get("diagram_version") or 0) == mermaid.version:
            return {"atomic_generation": current}
        if current["status"] == "completed" and int(current.get("diagram_version") or 0) == mermaid.version:
            return {"atomic_generation": current}
        generation_id = new_id("atomic_generation")
        start_record = ContextRecord(
            id=new_id("context"),
            record_type="atomic_generation_started",
            project_id=objective.project_id,
            objective_id=objective.id,
            visibility="operator_visible",
            author_type="system",
            content=f"Started generating atomic units from Mermaid v{mermaid.version}.",
            metadata={"generation_id": generation_id, "diagram_version": mermaid.version},
        )
        self.store.create_context_record(start_record)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Action receipt: Generating atomic units from accepted flowchart v{mermaid.version}.",
                metadata={"kind": "atomic_generation", "status": "started", "generation_id": generation_id, "diagram_version": mermaid.version},
            )
        )

        def worker() -> None:
            self._run_atomic_generation(objective.id, generation_id, mermaid.version)

        if async_mode:
            _ATOMIC_GENERATION.start(objective.id, worker)
        else:
            worker()
        return {"atomic_generation": self._atomic_generation_state(objective.id)}

    def _atomic_generation_is_stale(self, generation: dict[str, object], objective_id: str = "") -> bool:
        if generation.get("status") != "running":
            return False
        # If the in-memory coordinator thread is still alive, it's not stale
        if objective_id and objective_id in _ATOMIC_GENERATION._running:
            return False
        last_activity_at = str(generation.get("last_activity_at") or "")
        if not last_activity_at:
            return False
        try:
            last_activity = _dt.datetime.fromisoformat(last_activity_at)
        except ValueError:
            return False
        age_seconds = (_dt.datetime.now(_dt.timezone.utc) - last_activity).total_seconds()
        # LLM calls can take several minutes; 5 minutes is a reasonable staleness threshold
        return age_seconds > 300

    def _mark_atomic_generation_interrupted(self, objective: Objective, generation: dict[str, object]) -> None:
        generation_id = str(generation.get("generation_id") or "")
        if not generation_id:
            return
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_failed",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Atomic generation was interrupted before publishing units. The harness can resume from the accepted flowchart.",
                metadata={
                    "generation_id": generation_id,
                    "diagram_version": generation.get("diagram_version"),
                    "interrupted": True,
                },
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Action receipt: Atomic generation was interrupted. Resuming from the accepted flowchart.",
                metadata={
                    "kind": "atomic_generation",
                    "status": "interrupted",
                    "generation_id": generation_id,
                    "diagram_version": generation.get("diagram_version"),
                },
            )
        )

    def _maybe_resume_atomic_generation(self, objective_id: str) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        if mermaid is None or mermaid.status != MermaidStatus.FINISHED:
            return
        generation = self._atomic_generation_state(objective_id)
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective_id]
        if generation.get("status") == "completed":
            return
        if generation.get("status") == "running" and not self._atomic_generation_is_stale(generation, objective_id):
            return
        if linked_tasks:
            return
        self.queue_atomic_generation(objective_id, async_mode=not bool(getattr(self.ctx, "is_test", False)))

    def queue_objective_review(self, objective_id: str, *, async_mode: bool = True) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        current = self._objective_review_state(objective_id)
        if current["status"] == "running" and self._objective_review_is_stale(current, objective_id):
            self._mark_objective_review_interrupted(objective, current)
            current = self._objective_review_state(objective_id)
        if current["status"] == "running":
            return {"objective_review_state": current}
        review_summary = self._promotion_review_for_objective(objective_id, [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective_id])
        if not review_summary["ready"]:
            return {"objective_review_state": current}
        if not bool(review_summary.get("can_start_new_round", False)):
            return {"objective_review_state": current}
        review_id = new_id("objective_review")
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_review_started",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Started automatic objective promotion review.",
                metadata={"review_id": review_id},
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Action receipt: Starting automatic objective promotion review.",
                metadata={"kind": "objective_review", "status": "started", "review_id": review_id},
            )
        )

        def worker() -> None:
            self._run_objective_review(objective.id, review_id)

        if async_mode:
            _OBJECTIVE_REVIEW.start(objective.id, worker)
        else:
            worker()
        return {"objective_review_state": self._objective_review_state(objective.id)}

    def _objective_review_is_stale(self, review_state: dict[str, object], objective_id: str = "") -> bool:
        if review_state.get("status") != "running":
            return False
        if objective_id and objective_id in _OBJECTIVE_REVIEW._running:
            return False
        last_activity_at = str(review_state.get("last_activity_at") or "")
        if not last_activity_at:
            return False
        try:
            last_activity = _dt.datetime.fromisoformat(last_activity_at)
        except ValueError:
            return False
        age_seconds = (_dt.datetime.now(_dt.timezone.utc) - last_activity).total_seconds()
        return age_seconds > 300

    def _mark_objective_review_interrupted(self, objective: Objective, review_state: dict[str, object]) -> None:
        review_id = str(review_state.get("review_id") or "")
        if not review_id:
            return
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_review_failed",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Objective promotion review was interrupted before reviewer packets were recorded. The harness can restart the round.",
                metadata={"review_id": review_id, "interrupted": True},
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Action receipt: Objective promotion review was interrupted and is eligible for restart.",
                metadata={"kind": "objective_review", "status": "interrupted", "review_id": review_id},
            )
        )

    def _objective_review_state(self, objective_id: str) -> dict[str, object]:
        starts = self.store.list_context_records(objective_id=objective_id, record_type="objective_review_started")
        if not starts:
            return {"status": "idle", "review_id": "", "started_at": "", "completed_at": "", "failed_at": "", "last_activity_at": ""}
        start = starts[-1]
        review_id = str(start.metadata.get("review_id") or start.id)
        completed = next(
            (
                record
                for record in reversed(self.store.list_context_records(objective_id=objective_id, record_type="objective_review_completed"))
                if str(record.metadata.get("review_id") or "") == review_id
            ),
            None,
        )
        failed = next(
            (
                record
                for record in reversed(self.store.list_context_records(objective_id=objective_id, record_type="objective_review_failed"))
                if str(record.metadata.get("review_id") or "") == review_id
            ),
            None,
        )
        packets = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="objective_review_packet")
            if str(record.metadata.get("review_id") or "") == review_id
        ]
        status = "running"
        if failed is not None:
            status = "failed"
        elif completed is not None:
            status = "completed"
        related = [start.created_at]
        related.extend(record.created_at for record in packets)
        if completed is not None:
            related.append(completed.created_at)
        if failed is not None:
            related.append(failed.created_at)
        return {
            "status": status,
            "review_id": review_id,
            "started_at": start.created_at.isoformat(),
            "completed_at": completed.created_at.isoformat() if completed is not None else "",
            "failed_at": failed.created_at.isoformat() if failed is not None else "",
            "last_activity_at": max(related).isoformat() if related else "",
            "packet_count": len(packets),
            "error": failed.content if failed is not None else "",
        }

    def _maybe_resume_objective_review(self, objective_id: str) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective_id]
        review_summary = self._promotion_review_for_objective(objective_id, linked_tasks)
        review_state = self._objective_review_state(objective_id)
        if review_state.get("status") == "running" and self._objective_review_is_stale(review_state, objective_id):
            self._mark_objective_review_interrupted(objective, review_state)
            review_summary = self._promotion_review_for_objective(objective_id, linked_tasks)
            review_state = self._objective_review_state(objective_id)
        latest_round = (review_summary.get("review_rounds") or [None])[0]
        latest_round_status = str(latest_round.get("status") or "") if isinstance(latest_round, dict) else ""
        if isinstance(latest_round, dict) and latest_round.get("review_id"):
            review_id = str(latest_round.get("review_id") or "")
            restarted_any = False
            for task in linked_tasks:
                metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
                remediation = metadata.get("objective_review_remediation") if isinstance(metadata.get("objective_review_remediation"), dict) else None
                if (
                    task.status == TaskStatus.FAILED
                    and remediation is not None
                    and str(remediation.get("review_id") or "") == review_id
                ):
                    restarted_any = self._auto_retry_restart_safe_failed_task(task) or restarted_any
            if restarted_any:
                return
        if objective.status != ObjectiveStatus.RESOLVED and latest_round_status not in {"ready_for_rerun", "failed"}:
            return
        if (
            isinstance(latest_round, dict)
            and bool(latest_round.get("needs_remediation"))
            and latest_round.get("review_id")
            and int((latest_round.get("remediation_counts") or {}).get("active", 0) or 0) == 0
            and int((latest_round.get("remediation_counts") or {}).get("pending", 0) or 0) == 0
            and int((latest_round.get("remediation_counts") or {}).get("total", 0) or 0) == 0
        ):
            packets = [
                {
                    "reviewer": str(packet.get("reviewer") or ""),
                    "dimension": str(packet.get("dimension") or ""),
                    "verdict": str(packet.get("verdict") or ""),
                    "summary": str(packet.get("summary") or ""),
                    "findings": list(packet.get("findings") or []),
                }
                for packet in list(latest_round.get("packets") or [])
            ]
            self._create_objective_review_remediation_tasks(objective, str(latest_round.get("review_id") or ""), packets)
            return
        if isinstance(latest_round, dict) and latest_round.get("review_id"):
            self._record_objective_review_worker_responses(objective, latest_round)
        if not review_summary["ready"]:
            return
        if review_state["status"] == "running" and objective_id in _OBJECTIVE_REVIEW._running:
            return
        if not bool(review_summary.get("can_start_new_round", False)):
            return
        self.queue_objective_review(objective_id, async_mode=not bool(getattr(self.ctx, "is_test", False)))

    def _run_objective_review(self, objective_id: str, review_id: str) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        try:
            linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
            previous_review = self._promotion_review_for_objective(objective_id, linked_tasks)
            packets = self._generate_objective_review_packets(objective_id, review_id)
            packet_record_ids: list[str] = []
            for packet in packets:
                packet_record = ContextRecord(
                    id=new_id("context"),
                    record_type="objective_review_packet",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=str(packet["summary"]),
                    metadata={
                        "review_id": review_id,
                        "reviewer": packet["reviewer"],
                        "dimension": packet["dimension"],
                        "verdict": packet["verdict"],
                        "progress_status": packet.get("progress_status"),
                        "severity": packet.get("severity"),
                        "owner_scope": packet.get("owner_scope"),
                        "findings": packet["findings"],
                        "evidence": packet["evidence"],
                        "required_artifact_type": packet.get("required_artifact_type"),
                        "artifact_schema": packet.get("artifact_schema"),
                        "evidence_contract": packet.get("evidence_contract"),
                        "closure_criteria": packet.get("closure_criteria"),
                        "evidence_required": packet.get("evidence_required"),
                        "repeat_reason": packet.get("repeat_reason"),
                        "llm_usage": packet.get("llm_usage"),
                        "llm_usage_reported": packet.get("llm_usage_reported"),
                        "llm_usage_source": packet.get("llm_usage_source"),
                        "backend": packet.get("backend"),
                        "prompt_path": packet.get("prompt_path"),
                        "response_path": packet.get("response_path"),
                        "review_task_id": packet.get("review_task_id"),
                        "review_run_id": packet.get("review_run_id"),
                    },
                )
                self.store.create_context_record(packet_record)
                packet["packet_record_id"] = packet_record.id
                packet_record_ids.append(packet_record.id)
            completed_record = ContextRecord(
                id=new_id("context"),
                record_type="objective_review_completed",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Completed automatic objective review with {len(packets)} reviewer packet(s).",
                metadata={"review_id": review_id, "packet_count": len(packets)},
            )
            self.store.create_context_record(completed_record)
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="action_receipt",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Action receipt: Objective promotion review generated {len(packets)} reviewer packet(s).",
                    metadata={"kind": "objective_review", "status": "completed", "review_id": review_id, "packet_count": len(packets)},
                )
            )
            created_task_ids = self._create_objective_review_remediation_tasks(objective, review_id, packets)
            self._record_objective_review_cycle_artifact(
                objective=objective,
                review_id=review_id,
                packet_record_ids=packet_record_ids,
                completed_record=completed_record,
                linked_task_ids=created_task_ids,
            )
            self._record_objective_review_reviewer_rebuttals(
                objective=objective,
                review_id=review_id,
                previous_review=previous_review,
                current_packets=packets,
            )
            if created_task_ids:
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="action_receipt",
                        project_id=objective.project_id,
                        objective_id=objective.id,
                        visibility="operator_visible",
                        author_type="system",
                        content=f"Action receipt: Objective review created {len(created_task_ids)} remediation task(s) and returned the objective to Atomic.",
                        metadata={
                            "kind": "objective_review",
                            "status": "remediation_created",
                            "review_id": review_id,
                            "task_ids": created_task_ids,
                        },
                    )
                )
        except Exception as exc:
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="objective_review_failed",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Automatic objective review failed: {exc}",
                    metadata={"review_id": review_id},
                )
            )

    def _generate_objective_review_packets(self, objective_id: str, review_id: str) -> list[dict[str, object]]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return []
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective_id]
        objective_payload = self._promotion_review_for_objective(objective_id, linked_tasks)
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is not None and getattr(llm_router, "executors", {}):
            prompt = self._build_objective_review_prompt(objective, objective_payload, linked_tasks)
            run_dir = self.workspace_root / "ui_promotion_review" / objective.id / review_id
            run_dir.mkdir(parents=True, exist_ok=True)
            task = Task(
                id=new_id("objective_review_task"),
                project_id=objective.project_id,
                title=f"Generate objective review packets for {objective.title}",
                objective="Review the completed objective from multiple promotion dimensions.",
                strategy="objective_review",
                status=TaskStatus.COMPLETED,
            )
            run = Run(
                id=new_id("objective_review_run"),
                task_id=task.id,
                status=RunStatus.COMPLETED,
                attempt=1,
                summary=f"Objective review for {objective.id}",
            )
            try:
                result, backend = llm_router.execute(LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir))
                parsed = self._parse_objective_review_response(result.response_text, objective_payload=objective_payload)
                if parsed:
                    llm_usage, usage_reported, usage_source = self._objective_review_usage_details(result.diagnostics if isinstance(result.diagnostics, dict) else {}, task_id=task.id, run_id=run.id)
                    for packet in parsed:
                        packet["backend"] = backend
                        packet["prompt_path"] = str(result.prompt_path)
                        packet["response_path"] = str(result.response_path)
                        packet["llm_usage"] = llm_usage
                        packet["llm_usage_reported"] = usage_reported
                        packet["llm_usage_source"] = usage_source
                        packet["review_task_id"] = task.id
                        packet["review_run_id"] = run.id
                    return parsed
            except LLMExecutionError:
                pass
        return self._deterministic_objective_review_packets(objective_payload)

    def _objective_review_usage_details(
        self,
        diagnostics: dict[str, object],
        *,
        task_id: str,
        run_id: str,
    ) -> tuple[dict[str, object], bool, str]:
        usage = {
            "cost_usd": float(diagnostics.get("cost_usd", 0.0) or 0.0),
            "prompt_tokens": int(diagnostics.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(diagnostics.get("completion_tokens", 0) or 0),
            "total_tokens": int(diagnostics.get("total_tokens", 0) or 0),
            "latency_ms": float(diagnostics.get("latency_ms", 0.0) or 0.0),
            "shared_invocation": True,
        }
        if any(float(usage.get(key, 0) or 0) > 0 for key in ("cost_usd", "prompt_tokens", "completion_tokens", "total_tokens")):
            return usage, True, "diagnostics"
        telemetry = getattr(self.ctx, "telemetry", None)
        if telemetry is not None and hasattr(telemetry, "load_metrics"):
            try:
                metrics = telemetry.load_metrics()
            except Exception:
                metrics = []
            for item in metrics:
                attributes = item.get("attributes") if isinstance(item, dict) else {}
                if not isinstance(attributes, dict):
                    continue
                if str(attributes.get("task_id") or "") != task_id or str(attributes.get("run_id") or "") != run_id:
                    continue
                name = str(item.get("name") or "")
                value = float(item.get("value", 0.0) or 0.0)
                if name == "llm_cost_usd":
                    usage["cost_usd"] = value
                elif name == "llm_prompt_tokens":
                    usage["prompt_tokens"] = int(value)
                elif name == "llm_completion_tokens":
                    usage["completion_tokens"] = int(value)
                elif name == "llm_total_tokens":
                    usage["total_tokens"] = int(value)
                elif name == "llm_execute_duration_ms":
                    usage["latency_ms"] = max(float(usage.get("latency_ms", 0.0) or 0.0), value)
        if any(float(usage.get(key, 0) or 0) > 0 for key in ("cost_usd", "prompt_tokens", "completion_tokens", "total_tokens")):
            return usage, True, "telemetry"
        if float(usage.get("latency_ms", 0.0) or 0.0) > 0:
            usage["reported"] = False
            usage["missing_reason"] = "backend_did_not_report_token_usage"
            return usage, False, "telemetry_latency_only"
        return {
            "shared_invocation": True,
            "reported": False,
            "missing_reason": "backend_did_not_report_token_usage",
        }, False, "unreported"

    def _normalize_objective_review_usage_metadata(
        self,
        metadata: dict[str, object],
    ) -> tuple[dict[str, object], bool, str]:
        usage = dict(metadata.get("llm_usage") or {}) if isinstance(metadata.get("llm_usage"), dict) else {}
        source = str(metadata.get("llm_usage_source") or "").strip()
        raw_reported = metadata.get("llm_usage_reported")
        if isinstance(raw_reported, bool):
            reported = raw_reported
        else:
            reported = True
            if bool(usage.get("shared_invocation")) and not any(
                float(usage.get(key, 0) or 0) > 0 for key in ("cost_usd", "prompt_tokens", "completion_tokens", "total_tokens")
            ):
                reported = False
                if not source:
                    source = "unreported"
                usage.setdefault("reported", False)
                usage.setdefault("missing_reason", "backend_did_not_report_token_usage")
        return usage, reported, source

    def _create_objective_review_remediation_tasks(
        self,
        objective: Objective,
        review_id: str,
        packets: list[dict[str, object]],
    ) -> list[str]:
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
        existing_dimensions = set()
        for task in linked_tasks:
            metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
            remediation = metadata.get("objective_review_remediation") if isinstance(metadata.get("objective_review_remediation"), dict) else None
            if remediation and str(remediation.get("review_id") or "") == review_id:
                existing_dimensions.add(str(remediation.get("dimension") or ""))
        created: list[str] = []
        for packet in packets:
            verdict = str(packet.get("verdict") or "").strip()
            dimension = str(packet.get("dimension") or "").strip()
            if verdict not in {"concern", "remediation_required"} or not dimension or dimension in existing_dimensions:
                continue
            findings = [str(item).strip() for item in list(packet.get("findings") or []) if str(item).strip()]
            summary = str(packet.get("summary") or "").strip()
            evidence_contract = self._objective_review_evidence_contract(packet)
            artifact_type = str(evidence_contract.get("required_artifact_type") or "review_artifact")
            title = f"Produce {artifact_type.replace('_', ' ')} for {dimension.replace('_', ' ')} review finding"
            objective_text = self._build_objective_review_remediation_objective(
                summary=summary,
                findings=findings,
                evidence_contract=evidence_contract,
            )
            task = self.task_service.create_task_with_policy(
                project_id=objective.project_id,
                objective_id=objective.id,
                title=title,
                objective=objective_text,
                priority=objective.priority,
                parent_task_id=None,
                source_run_id=None,
                external_ref_type="objective_review",
                external_ref_id=f"{objective.id}:{review_id}:{dimension}",
                external_ref_metadata={
                    "objective_review_remediation": {
                        "review_id": review_id,
                        "dimension": dimension,
                        "reviewer": str(packet.get("reviewer") or ""),
                        "verdict": verdict,
                        "finding_record_id": str(packet.get("packet_record_id") or ""),
                        "evidence_contract": evidence_contract,
                    }
                },
                validation_profile="generic",
                validation_mode="default_focused",
                scope={},
                strategy="objective_review_remediation",
                max_attempts=3,
                required_artifacts=list(dict.fromkeys(["plan", "report", artifact_type])),
            )
            created.append(task.id)
            existing_dimensions.add(dimension)
        if created:
            self.store.update_objective_phase(objective.id)
        return created

    def _objective_review_evidence_contract(self, packet: dict[str, object]) -> dict[str, object]:
        contract = packet.get("evidence_contract") if isinstance(packet.get("evidence_contract"), dict) else {}
        required_artifact_type = str(
            contract.get("required_artifact_type") or packet.get("required_artifact_type") or ""
        ).strip()
        artifact_schema = self._normalize_objective_review_artifact_schema(
            contract.get("artifact_schema") if contract else packet.get("artifact_schema"),
            required_artifact_type=required_artifact_type,
            dimension=str(packet.get("dimension") or ""),
        ) or {}
        closure_criteria = str(contract.get("closure_criteria") or packet.get("closure_criteria") or "").strip()
        evidence_required = str(contract.get("evidence_required") or packet.get("evidence_required") or "").strip()
        return {
            "required_artifact_type": required_artifact_type,
            "artifact_schema": artifact_schema,
            "closure_criteria": closure_criteria,
            "evidence_required": evidence_required,
        }

    def _build_objective_review_remediation_objective(
        self,
        *,
        summary: str,
        findings: list[str],
        evidence_contract: dict[str, object],
    ) -> str:
        artifact_type = str(evidence_contract.get("required_artifact_type") or "review_artifact")
        artifact_schema = evidence_contract.get("artifact_schema") if isinstance(evidence_contract.get("artifact_schema"), dict) else {}
        required_fields = [str(item).strip() for item in list(artifact_schema.get("required_fields") or []) if str(item).strip()]
        lines = [f"Produce the required review evidence artifact `{artifact_type}` for this objective-promotion finding."]
        if summary:
            lines.append(f"Reviewer summary: {summary}")
        if findings:
            lines.append("Findings:")
            lines.extend(f"- {item}" for item in findings)
        if evidence_contract.get("closure_criteria"):
            lines.append(f"Closure criteria: {evidence_contract['closure_criteria']}")
        if evidence_contract.get("evidence_required"):
            lines.append(f"Evidence required: {evidence_contract['evidence_required']}")
        if required_fields:
            lines.append("Artifact schema fields: " + ", ".join(required_fields))
        lines.append("Do not answer this generically. Produce the exact artifact type named above.")
        return "\n".join(lines)

    def _record_objective_review_cycle_artifact(
        self,
        *,
        objective: Objective,
        review_id: str,
        packet_record_ids: list[str],
        completed_record: ContextRecord,
        linked_task_ids: list[str],
    ) -> None:
        existing = [
            record
            for record in self.store.list_context_records(objective_id=objective.id, record_type="objective_review_cycle_artifact")
            if str(record.metadata.get("review_id") or "") == review_id
        ]
        if existing:
            return
        start_record = next(
            (
                record for record in reversed(self.store.list_context_records(objective_id=objective.id, record_type="objective_review_started"))
                if str(record.metadata.get("review_id") or "") == review_id
            ),
            None,
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_review_cycle_artifact",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Persisted first-class review cycle artifact for review {review_id}.",
                metadata={
                    "review_id": review_id,
                    "start_event": {
                        "record_id": start_record.id if start_record is not None else "",
                        "created_at": start_record.created_at.isoformat() if start_record is not None else "",
                    },
                    "packet_persistence_events": packet_record_ids,
                    "terminal_event": {
                        "record_id": completed_record.id,
                        "created_at": completed_record.created_at.isoformat(),
                    },
                    "linked_outcome": {
                        "kind": "remediation_created" if linked_task_ids else "review_clear",
                        "task_ids": linked_task_ids,
                    },
                },
            )
        )

    def _record_objective_review_worker_responses(self, objective: Objective, latest_round: dict[str, object]) -> None:
        review_id = str(latest_round.get("review_id") or "")
        if not review_id:
            return
        tasks = [
            task for task in self.store.list_tasks(objective.project_id)
            if task.objective_id == objective.id
            and task.strategy == "objective_review_remediation"
            and isinstance(task.external_ref_metadata, dict)
            and isinstance(task.external_ref_metadata.get("objective_review_remediation"), dict)
            and str(task.external_ref_metadata["objective_review_remediation"].get("review_id") or "") == review_id
            and task.status == TaskStatus.COMPLETED
        ]
        existing_keys = {
            (
                str(record.metadata.get("review_id") or ""),
                str(record.metadata.get("task_id") or ""),
                str(record.metadata.get("run_id") or ""),
            )
            for record in self.store.list_context_records(objective_id=objective.id, record_type="objective_review_worker_response")
        }
        for task in tasks:
            metadata = task.external_ref_metadata.get("objective_review_remediation") if isinstance(task.external_ref_metadata.get("objective_review_remediation"), dict) else {}
            runs = self.store.list_runs(task.id)
            run = runs[-1] if runs else None
            run_id = run.id if run is not None else ""
            key = (review_id, task.id, run_id)
            if key in existing_keys:
                continue
            evidence_contract = metadata.get("evidence_contract") if isinstance(metadata.get("evidence_contract"), dict) else {}
            required_artifact_type = str(evidence_contract.get("required_artifact_type") or "")
            artifacts = self.store.list_artifacts(run.id) if run is not None else []
            exact_artifact = next((artifact for artifact in artifacts if artifact.kind == required_artifact_type), artifacts[0] if artifacts else None)
            exact_payload = {
                "artifact_id": exact_artifact.id if exact_artifact is not None else "",
                "kind": exact_artifact.kind if exact_artifact is not None else "",
                "path": exact_artifact.path if exact_artifact is not None else "",
                "summary": exact_artifact.summary if exact_artifact is not None else "",
            }
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="objective_review_worker_response",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    task_id=task.id,
                    run_id=run.id if run is not None else None,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Worker response recorded for review {review_id} {metadata.get('dimension') or ''}.",
                    metadata={
                        "review_id": review_id,
                        "task_id": task.id,
                        "run_id": run.id if run is not None else "",
                        "dimension": str(metadata.get("dimension") or ""),
                        "finding_record_id": str(metadata.get("finding_record_id") or ""),
                        "exact_artifact_produced": exact_payload,
                        "path": exact_payload["path"],
                        "record_id": exact_payload["artifact_id"],
                        "closure_mapping": self._map_artifact_to_closure(evidence_contract, exact_payload),
                        "closure_criteria": str(evidence_contract.get("closure_criteria") or ""),
                        "required_artifact_type": required_artifact_type,
                    },
                )
            )

    def _map_artifact_to_closure(self, evidence_contract: dict[str, object], exact_payload: dict[str, object]) -> str:
        artifact_type = str(evidence_contract.get("required_artifact_type") or "")
        closure = str(evidence_contract.get("closure_criteria") or "")
        path = str(exact_payload.get("path") or "")
        if not path:
            return f"No artifact was found for required artifact type `{artifact_type}`. Closure criteria remain open: {closure}".strip()
        return f"Artifact `{artifact_type}` was produced at {path}. This response maps directly to closure criteria: {closure}".strip()

    def _record_objective_review_reviewer_rebuttals(
        self,
        *,
        objective: Objective,
        review_id: str,
        previous_review: dict[str, object],
        current_packets: list[dict[str, object]],
    ) -> None:
        prior_rounds = list(previous_review.get("review_rounds") or [])
        if not prior_rounds:
            return
        prior_round = prior_rounds[0] if isinstance(prior_rounds[0], dict) else {}
        prior_review_id = str(prior_round.get("review_id") or "")
        if not prior_review_id:
            return
        current_by_dimension = {
            str(packet.get("dimension") or ""): packet
            for packet in current_packets
            if str(packet.get("dimension") or "")
        }
        worker_by_dimension = {
            str(record.metadata.get("dimension") or ""): record
            for record in self.store.list_context_records(objective_id=objective.id, record_type="objective_review_worker_response")
            if str(record.metadata.get("review_id") or "") == prior_review_id and str(record.metadata.get("dimension") or "")
        }
        for packet in list(prior_round.get("packets") or []):
            if str(packet.get("verdict") or "") not in {"concern", "remediation_required"}:
                continue
            dimension = str(packet.get("dimension") or "")
            outcome, reason = self._classify_objective_review_rebuttal(
                packet,
                current_by_dimension.get(dimension),
                worker_by_dimension.get(dimension),
            )
            if outcome not in _OBJECTIVE_REVIEW_REBUTTAL_OUTCOMES:
                continue
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="objective_review_reviewer_rebuttal",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Reviewer rebuttal for {dimension}: {outcome}.",
                    metadata={
                        "review_id": review_id,
                        "prior_review_id": prior_review_id,
                        "dimension": dimension,
                        "outcome": outcome,
                        "reason": reason,
                    },
                )
            )

    def _classify_objective_review_rebuttal(
        self,
        prior_packet: dict[str, object],
        current_packet: dict[str, object] | None,
        worker_response: ContextRecord | None,
    ) -> tuple[str, str]:
        prior_contract = self._objective_review_evidence_contract(prior_packet)
        expected_type = str(prior_contract.get("required_artifact_type") or "")
        if current_packet and str(current_packet.get("verdict") or "") == "pass":
            return "accepted", "Current review packet accepted the evidence and cleared the finding."
        if worker_response is None:
            return "evidence_not_found", "No worker response record was found for the prior finding."
        produced = worker_response.metadata.get("exact_artifact_produced") if isinstance(worker_response.metadata.get("exact_artifact_produced"), dict) else {}
        produced_type = str(produced.get("kind") or "")
        if not str(produced.get("path") or ""):
            return "evidence_not_found", "Worker response did not point to a persisted artifact."
        if expected_type and produced_type and produced_type != expected_type:
            return "wrong_artifact_type", f"Worker produced `{produced_type}` but the contract required `{expected_type}`."
        schema = prior_contract.get("artifact_schema") if isinstance(prior_contract.get("artifact_schema"), dict) else {}
        required_fields = [str(item).strip().lower() for item in list(schema.get("required_fields") or []) if str(item).strip()]
        if any(field in {"terminal_event", "completed_at"} for field in required_fields):
            mapping = str(worker_response.metadata.get("closure_mapping") or "")
            if "No artifact was found" in mapping:
                return "missing_terminal_event", "The required terminal event evidence was not persisted."
        return "artifact_incomplete", "A response artifact exists, but the reviewer still did not accept it as closing the contract."

    def _run_atomic_generation(self, objective_id: str, generation_id: str, diagram_version: int) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        try:
            self._record_atomic_generation_progress(
                objective,
                generation_id,
                diagram_version,
                phase="reading accepted flowchart",
                content=f"Reading accepted Mermaid v{diagram_version} before decomposition.",
            )
            self._record_atomic_generation_progress(
                objective,
                generation_id,
                diagram_version,
                phase="deriving candidate units",
                content="Deriving candidate atomic units from the accepted flowchart.",
            )
            units = self._derive_atomic_units(objective_id, generation_id=generation_id, diagram_version=diagram_version)
            self._record_atomic_generation_progress(
                objective,
                generation_id,
                diagram_version,
                phase="publishing units",
                content=f"Publishing {len(units)} atomic units to the objective.",
            )
            for index, unit in enumerate(units, start=1):
                task = self.task_service.create_task_with_policy(
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    title=str(unit["title"]),
                    objective=str(unit["objective"]),
                    priority=objective.priority,
                    parent_task_id=None,
                    source_run_id=None,
                    external_ref_type=None,
                    external_ref_id=None,
                    validation_profile="generic",
                    validation_mode="lightweight_operator",
                    scope={},
                    strategy=str(unit.get("strategy") or "atomic_from_mermaid"),
                    max_attempts=3,
                    required_artifacts=["plan", "report"],
                )
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="atomic_unit_generated",
                        project_id=objective.project_id,
                        objective_id=objective.id,
                        task_id=task.id,
                        visibility="operator_visible",
                        author_type="system",
                        content=f"Generated atomic unit {index}: {task.title}",
                        metadata={
                            "generation_id": generation_id,
                            "diagram_version": diagram_version,
                            "order": index,
                            "task_id": task.id,
                            "title": task.title,
                            "objective": task.objective,
                            "rationale": str(unit.get("rationale") or ""),
                            "strategy": task.strategy,
                        },
                    )
                )
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="action_receipt",
                        project_id=objective.project_id,
                        objective_id=objective.id,
                        visibility="operator_visible",
                        author_type="system",
                        content=f"Action receipt: Published atomic unit {index}: {task.title}",
                        metadata={
                            "kind": "atomic_generation",
                            "status": "publishing",
                            "generation_id": generation_id,
                            "diagram_version": diagram_version,
                            "order": index,
                            "task_id": task.id,
                        },
                    )
                )
                time.sleep(0.12)
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="atomic_generation_completed",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Generated {len(units)} atomic units from Mermaid v{diagram_version}.",
                    metadata={"generation_id": generation_id, "diagram_version": diagram_version, "unit_count": len(units)},
                )
            )
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="action_receipt",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Action receipt: Atomic generation complete. {len(units)} units are ready for review.",
                    metadata={"kind": "atomic_generation", "status": "completed", "generation_id": generation_id, "unit_count": len(units)},
                )
            )
            self.store.update_objective_status(objective.id, ObjectiveStatus.EXECUTING)
            if self.auto_resume_atomic_generation:
                _BACKGROUND_SUPERVISOR.start(objective.project_id, self.ctx.engine, watch=True)
        except Exception as exc:
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="atomic_generation_failed",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Atomic generation failed: {exc}",
                    metadata={"generation_id": generation_id, "diagram_version": diagram_version},
                )
            )
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="action_receipt",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content="Action receipt: Atomic generation failed. Ask the harness to retry or revise the flowchart decomposition.",
                    metadata={"kind": "atomic_generation", "status": "failed", "generation_id": generation_id},
                )
            )
            self.store.update_objective_status(objective.id, ObjectiveStatus.PAUSED)

    def _record_atomic_generation_progress(
        self,
        objective: Objective,
        generation_id: str,
        diagram_version: int,
        *,
        phase: str,
        content: str,
    ) -> None:
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_progress",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=content,
                metadata={"generation_id": generation_id, "diagram_version": diagram_version, "phase": phase},
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Action receipt: Atomic generation phase changed to {phase}.",
                metadata={
                    "kind": "atomic_generation",
                    "status": "progress",
                    "generation_id": generation_id,
                    "diagram_version": diagram_version,
                    "phase": phase,
                },
            )
        )

    def _derive_atomic_units(self, objective_id: str, *, generation_id: str = "", diagram_version: int = 0) -> list[dict[str, str]]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return []
        intent_model = self.store.latest_intent_model(objective_id)
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        comments = self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")[-12:]
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is not None and getattr(llm_router, "executors", {}):
            repo_context = self._gather_repo_context(objective.project_id)
            units = self._iterative_atomic_decomposition(
                objective, intent_model, mermaid, comments, llm_router, repo_context,
                generation_id=generation_id or new_id("atomic_gen"),
            )
            if units:
                return units

        return []

    def _gather_repo_context(self, project_id: str) -> str:
        """Gather repo file tree and key file snippets for grounding atomic decomposition."""
        project = self.store.get_project(project_id)
        source_root = None
        if project and project.adapter_name == "current_repo_git_worktree":
            configured = os.environ.get("ACCRUVIA_SOURCE_REPO_ROOT")
            if configured:
                source_root = Path(configured).resolve()
            else:
                try:
                    result = subprocess.run(
                        ["git", "rev-parse", "--show-toplevel"],
                        check=True, capture_output=True, text=True,
                    )
                    source_root = Path(result.stdout.strip())
                except Exception:
                    pass
        if source_root is None:
            source_root = Path(__file__).resolve().parents[2]
        if not source_root.is_dir():
            return ""
        parts: list[str] = []
        try:
            result = subprocess.run(
                ["git", "-C", str(source_root), "ls-files", "--cached", "--others", "--exclude-standard"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                files = result.stdout.strip().splitlines()[:200]
                parts.append("Repository file tree (first 200 files):\n" + "\n".join(files))
        except Exception:
            pass
        for key_file in ["README.md", "CLAUDE.md", "pyproject.toml", "package.json"]:
            path = source_root / key_file
            if path.is_file():
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")[:3000]
                    parts.append(f"\n--- {key_file} (first 3000 chars) ---\n{content}")
                except Exception:
                    pass
        return "\n".join(parts)

    def _iterative_atomic_decomposition(
        self,
        objective: Objective,
        intent_model,
        mermaid,
        comments: list[ContextRecord],
        llm_router,
        repo_context: str,
        hard_ceiling: int = 50,
        generation_id: str = "",
    ) -> list[dict[str, str]]:
        """Multi-pass atomic decomposition: generate, critique, refine until the critique accepts."""
        if not generation_id:
            generation_id = new_id("atomic_gen")
        run_dir = self.workspace_root / "ui_atomic" / objective.id / generation_id
        run_dir.mkdir(parents=True, exist_ok=True)
        diagram_version = int(getattr(mermaid, "version", 0))
        task_stub = Task(
            id=new_id("ui_atomic_task"),
            project_id=objective.project_id,
            objective_id=objective.id,
            title=f"Generate atomic units for {objective.title}",
            objective="Derive atomic units from accepted Mermaid.",
            strategy="ui_atomic_generation",
            status=TaskStatus.COMPLETED,
        )

        context_block = (
            f"Objective title: {objective.title}\n"
            f"Objective summary: {objective.summary}\n"
            f"Intent summary: {intent_model.intent_summary if intent_model else ''}\n"
            f"Success definition: {intent_model.success_definition if intent_model else ''}\n"
            f"Non-negotiables: {json.dumps(intent_model.non_negotiables if intent_model else [])}\n"
            f"Accepted Mermaid:\n{mermaid.content if mermaid else ''}\n"
            f"Recent operator comments:\n{json.dumps([r.content for r in comments], indent=2)}\n"
        )
        if repo_context:
            context_block += f"\n{repo_context}\n"

        # Telemetry: log the full decomposition session start
        self._log_decomposition_telemetry(objective, generation_id, diagram_version, "session_start", {
            "hard_ceiling": hard_ceiling,
            "context_block_length": len(context_block),
            "repo_context_length": len(repo_context),
            "comment_count": len(comments),
            "has_intent_model": intent_model is not None,
            "has_mermaid": mermaid is not None,
        })

        current_units: list[dict[str, str]] = []
        round_num = 0
        critique_accepted = False
        coverage_accepted = False
        consecutive_stalls = 0

        while round_num < hard_ceiling:
            round_num += 1
            round_start = time.monotonic()

            self._record_atomic_generation_progress(
                objective, generation_id, diagram_version,
                phase=f"round {round_num}: {'generate' if round_num == 1 else 'critique + coverage + refine'}",
                content=f"Atomic decomposition round {round_num}.",
            )

            # ── Round 1: initial generation ──
            if round_num == 1:
                current_units = self._llm_generate_units(
                    llm_router, task_stub, run_dir, context_block,
                )
                round_elapsed = time.monotonic() - round_start
                self._log_decomposition_telemetry(objective, generation_id, diagram_version, "generate", {
                    "round": round_num,
                    "unit_count": len(current_units),
                    "unit_titles": [u.get("title", "") for u in current_units],
                    "elapsed_seconds": round(round_elapsed, 2),
                })
                self._write_round_artifact(run_dir, round_num, "generate", current_units)
                if not current_units:
                    self._log_decomposition_telemetry(objective, generation_id, diagram_version, "generate_empty", {
                        "round": round_num,
                        "note": "LLM returned zero units, falling back to regex",
                    })
                    break
                continue

            previous_titles = [u.get("title") for u in current_units]

            # ── Step A: atomicity critique ──
            critique_start = time.monotonic()
            critique = self._llm_critique_units(
                llm_router, task_stub, run_dir, context_block, current_units,
            )
            critique_elapsed = time.monotonic() - critique_start
            critique_accepted = bool(critique.get("accepted", False))

            self._log_decomposition_telemetry(objective, generation_id, diagram_version, "critique", {
                "round": round_num,
                "accepted": critique_accepted,
                "problem_count": len(critique.get("problems", [])),
                "problems": critique.get("problems", []),
                "suggestion_count": len(critique.get("suggestions", [])),
                "suggestions": critique.get("suggestions", []),
                "units_needing_split": critique.get("units_needing_split", []),
                "unit_count": len(current_units),
                "elapsed_seconds": round(critique_elapsed, 2),
            })
            self._write_round_artifact(run_dir, round_num, "critique", critique)

            # ── Step B: coverage / gap analysis ──
            coverage_start = time.monotonic()
            coverage = self._llm_coverage_analysis(
                llm_router, task_stub, run_dir, context_block, current_units,
            )
            coverage_elapsed = time.monotonic() - coverage_start
            coverage_accepted = bool(coverage.get("complete", False))

            self._log_decomposition_telemetry(objective, generation_id, diagram_version, "coverage", {
                "round": round_num,
                "complete": coverage_accepted,
                "gap_count": len(coverage.get("gaps", [])),
                "gaps": coverage.get("gaps", []),
                "uncovered_nodes": coverage.get("uncovered_mermaid_nodes", []),
                "uncovered_intents": coverage.get("uncovered_intent_concerns", []),
                "redundant_units": coverage.get("redundant_units", []),
                "unit_count": len(current_units),
                "elapsed_seconds": round(coverage_elapsed, 2),
            })
            self._write_round_artifact(run_dir, round_num, "coverage", coverage)

            # ── Check: both pass → done ──
            if critique_accepted and coverage_accepted:
                self._record_atomic_generation_progress(
                    objective, generation_id, diagram_version,
                    phase=f"accepted at round {round_num}",
                    content=f"Both critique and coverage passed after {round_num} rounds.",
                )
                self._log_decomposition_telemetry(objective, generation_id, diagram_version, "both_accepted", {
                    "round": round_num,
                    "unit_count": len(current_units),
                })
                break

            # ── Step C: refine (fix critique issues + fill gaps) ──
            refine_start = time.monotonic()
            current_units = self._llm_refine_units(
                llm_router, task_stub, run_dir, context_block, current_units,
                critique, coverage,
            )
            refine_elapsed = time.monotonic() - refine_start

            new_titles = [u.get("title") for u in current_units]
            units_changed = new_titles != previous_titles
            self._log_decomposition_telemetry(objective, generation_id, diagram_version, "refine", {
                "round": round_num,
                "unit_count_before": len(previous_titles),
                "unit_count_after": len(current_units),
                "units_changed": units_changed,
                "unit_titles": new_titles,
                "elapsed_seconds": round(refine_elapsed, 2),
            })
            self._write_round_artifact(run_dir, round_num, "refine", current_units)

            round_elapsed = time.monotonic() - round_start
            self._log_decomposition_telemetry(objective, generation_id, diagram_version, "round_complete", {
                "round": round_num,
                "total_round_seconds": round(round_elapsed, 2),
                "unit_count": len(current_units),
                "critique_accepted": critique_accepted,
                "coverage_accepted": coverage_accepted,
            })

            # Stall detection: consecutive rounds with no change
            if not units_changed:
                consecutive_stalls += 1
                self._log_decomposition_telemetry(objective, generation_id, diagram_version, "stall_detected", {
                    "round": round_num,
                    "consecutive_stalls": consecutive_stalls,
                    "note": "Refinement produced identical unit titles.",
                })
                if consecutive_stalls >= 3:
                    self._log_decomposition_telemetry(objective, generation_id, diagram_version, "stall_exit", {
                        "round": round_num,
                        "consecutive_stalls": consecutive_stalls,
                        "note": "Three consecutive stalls. Accepting current state.",
                    })
                    break
            else:
                consecutive_stalls = 0

        # Session summary telemetry
        self._log_decomposition_telemetry(objective, generation_id, diagram_version, "session_end", {
            "total_rounds": round_num,
            "critique_accepted": critique_accepted,
            "coverage_accepted": coverage_accepted,
            "hit_ceiling": round_num >= hard_ceiling,
            "stall_exit": consecutive_stalls >= 3,
            "final_unit_count": len(current_units),
            "final_unit_titles": [u.get("title", "") for u in current_units],
        })

        return current_units

    def _log_decomposition_telemetry(
        self, objective: Objective, generation_id: str, diagram_version: int,
        event_type: str, payload: dict[str, object],
    ) -> None:
        """Write a context record for every decomposition event — verbose by design."""
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_decomposition_telemetry",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Decomposition [{event_type}]: {json.dumps(payload, default=str)[:500]}",
                metadata={
                    "generation_id": generation_id,
                    "diagram_version": diagram_version,
                    "event_type": event_type,
                    **{k: v for k, v in payload.items()},
                },
            )
        )

    def _write_round_artifact(
        self, run_dir: Path, round_num: int, step: str, data: object,
    ) -> None:
        """Persist each round's output to disk for post-mortem forensics."""
        artifact_path = run_dir / f"round_{round_num:03d}_{step}.json"
        try:
            artifact_path.write_text(
                json.dumps(data, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _llm_call(self, llm_router, task_stub: Task, run_dir: Path, prompt: str) -> str:
        run = Run(
            id=new_id("ui_atomic_run"),
            task_id=task_stub.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary="Atomic generation LLM call",
        )
        result, _backend = llm_router.execute(
            LLMInvocation(
                task=task_stub, run=run, prompt=prompt, run_dir=run_dir,
                timeout_seconds_override=None,  # Use the global timeout policy
            ),
        )
        return result.response_text.strip()

    def _llm_generate_units(
        self, llm_router, task_stub: Task, run_dir: Path, context_block: str,
    ) -> list[dict[str, str]]:
        prompt = (
            "You are decomposing a software objective into ATOMIC implementation units.\n\n"
            "DEFINITION OF ATOMIC:\n"
            "An atomic unit is the smallest possible unit of work. Ideally it touches a single\n"
            "function. At most it touches one file or one tightly-coupled page of code. If a unit\n"
            "requires changes to multiple unrelated functions or files, it is NOT atomic — split it.\n"
            "Think: one function, one test, one reviewable diff.\n\n"
            "Each unit must be specific enough that a developer can start coding immediately without\n"
            "asking clarifying questions. Units must reference specific files, modules, or components\n"
            "from the repository.\n\n"
            "Rules:\n"
            "- Return JSON only: {\"units\": [...]}\n"
            "- Generate as many units as the objective requires. Do NOT cap or limit the count.\n"
            "  A complex objective may need 20, 30, or 50+ units. That is correct.\n"
            "- Each unit has keys: title, objective, rationale, strategy, files_involved\n"
            "- title: short imperative phrase naming the exact function or class\n"
            "  (e.g. 'Add retry counter to RunService.run_once' not 'Implement retry logic')\n"
            "- objective: 2-4 sentences. Name the exact file, class, and function to modify or create.\n"
            "  Describe the input/output contract and the acceptance test.\n"
            "- rationale: why this is a separate unit (what breaks if merged with another)\n"
            "- strategy: 'atomic_from_mermaid'\n"
            "- files_involved: list of specific file paths this unit will touch (1-2 files max)\n"
            "- Each unit must map to a node or edge in the accepted Mermaid flowchart\n"
            "- Units must not overlap. Each file/function change belongs to exactly one unit.\n"
            "- Order units by dependency: earlier units should not depend on later ones.\n"
            "- Prefer MORE smaller units over FEWER larger ones. 5-12 tiny units is better than 3 big ones.\n\n"
            f"{context_block}\n"
        )
        for attempt in range(1, 3):
            try:
                raw = self._llm_call(llm_router, task_stub, run_dir, prompt)
                if not raw:
                    (run_dir / f"generate_attempt_{attempt}_empty_response.txt").write_text(
                        "LLM returned empty response", encoding="utf-8",
                    )
                    continue
                (run_dir / f"generate_attempt_{attempt}_raw.txt").write_text(raw, encoding="utf-8")
                units = self._parse_units_json(raw)
                if units:
                    return units
                (run_dir / f"generate_attempt_{attempt}_parse_failed.txt").write_text(
                    f"Parsed 0 units from response ({len(raw)} chars):\n{raw[:2000]}", encoding="utf-8",
                )
            except Exception as exc:
                (run_dir / f"generate_attempt_{attempt}_error.txt").write_text(
                    f"{type(exc).__name__}: {exc}", encoding="utf-8",
                )
        return []

    def _llm_critique_units(
        self, llm_router, task_stub: Task, run_dir: Path,
        context_block: str, units: list[dict[str, str]],
    ) -> dict[str, object]:
        units_json = json.dumps(units, indent=2)
        prompt = (
            "You are reviewing atomic implementation units for quality, specificity, and atomicity.\n\n"
            "DEFINITION OF ATOMIC:\n"
            "An atomic unit is the smallest possible unit of work — ideally a single function,\n"
            "at most one file or one page of tightly-coupled code. If a unit touches multiple\n"
            "unrelated functions or files, it is NOT atomic and must be split.\n\n"
            "A unit is GOOD if:\n"
            "- It names specific files, classes, and functions from the repository\n"
            "- A developer can start coding from it without asking clarifying questions\n"
            "- It touches at most 1-2 files and ideally one function\n"
            "- Its acceptance criteria are concrete and testable\n\n"
            "A unit is BAD if:\n"
            "- It is vague or generic (e.g. 'implement the handler' without naming which file/function)\n"
            "- It bundles multiple independent changes that could be separate units\n"
            "- It overlaps with another unit\n"
            "- It doesn't reference specific files/functions/classes from the repository\n\n"
            "Return JSON only:\n"
            "{\n"
            "  \"accepted\": bool,\n"
            "  \"problems\": [str],\n"
            "  \"suggestions\": [str],\n"
            "  \"units_needing_split\": [\n"
            "    {\"unit_title\": str, \"reason\": str, \"suggested_splits\": [str]}\n"
            "  ]\n"
            "}\n"
            "- accepted: true only if ALL units are specific, atomic, and actionable\n"
            "- problems: list of specific issues with unit numbers\n"
            "- suggestions: concrete improvements referencing actual repo files\n"
            "- units_needing_split: units that bundle too much work and should become 2+ separate units.\n"
            "  For each, give the title, why it needs splitting, and suggested split titles.\n\n"
            f"Units to review:\n{units_json}\n\n"
            f"Context:\n{context_block}\n"
        )
        try:
            raw = self._llm_call(llm_router, task_stub, run_dir, prompt)
            parsed = self._extract_json(raw)
            return {
                "accepted": bool(parsed.get("accepted", False)),
                "problems": list(parsed.get("problems", [])),
                "suggestions": list(parsed.get("suggestions", [])),
                "units_needing_split": list(parsed.get("units_needing_split", [])),
            }
        except Exception:
            return {"accepted": False, "problems": ["Failed to parse critique"], "suggestions": [], "units_needing_split": []}

    def _llm_coverage_analysis(
        self, llm_router, task_stub: Task, run_dir: Path,
        context_block: str, units: list[dict[str, str]],
    ) -> dict[str, object]:
        """Check whether the task set fully covers the Mermaid diagram and intent model."""
        units_json = json.dumps(units, indent=2)
        prompt = (
            "You are a red-team reviewer checking whether a set of implementation tasks\n"
            "COMPLETELY covers the intent behind an objective.\n\n"
            "Your job is adversarial: look for GAPS. Assume the developer will implement\n"
            "exactly what the tasks say and nothing more. If the intent cannot be fully\n"
            "accomplished because no task addresses a concern, that is a gap.\n\n"
            "Specifically check:\n"
            "1. MERMAID NODE COVERAGE: Every node and decision branch in the accepted Mermaid\n"
            "   flowchart must be addressed by at least one task. List any uncovered nodes.\n"
            "2. INTENT COVERAGE: The intent summary, success definition, and non-negotiables\n"
            "   describe what the operator actually wants. If a concern from the intent is not\n"
            "   addressed by any task, that is a gap.\n"
            "3. EDGE CASES: Are there error paths, rollback scenarios, or boundary conditions\n"
            "   in the Mermaid that no task handles?\n"
            "4. INTEGRATION: Do the tasks collectively produce a working whole? Are there\n"
            "   missing glue tasks (e.g. wiring a new function into an existing call site)?\n"
            "5. REDUNDANCY: Are any tasks doing the same thing? Flag duplicates.\n\n"
            "Return JSON only:\n"
            "{\n"
            "  \"complete\": bool,\n"
            "  \"gaps\": [\n"
            "    {\"description\": str, \"source\": str, \"suggested_task\": str}\n"
            "  ],\n"
            "  \"uncovered_mermaid_nodes\": [str],\n"
            "  \"uncovered_intent_concerns\": [str],\n"
            "  \"redundant_units\": [\n"
            "    {\"units\": [str], \"reason\": str}\n"
            "  ]\n"
            "}\n"
            "- complete: true only if there are ZERO gaps and ZERO uncovered nodes/concerns\n"
            "- gaps: specific missing pieces. For each, describe what's missing, where in the\n"
            "  Mermaid or intent it comes from (source), and suggest a task title to fill it.\n"
            "- uncovered_mermaid_nodes: Mermaid node labels that no task addresses\n"
            "- uncovered_intent_concerns: intent/success/non-negotiable items no task addresses\n"
            "- redundant_units: groups of task titles that overlap\n\n"
            f"Tasks to review:\n{units_json}\n\n"
            f"Context:\n{context_block}\n"
        )
        try:
            raw = self._llm_call(llm_router, task_stub, run_dir, prompt)
            parsed = self._extract_json(raw)
            return {
                "complete": bool(parsed.get("complete", False)),
                "gaps": list(parsed.get("gaps", [])),
                "uncovered_mermaid_nodes": list(parsed.get("uncovered_mermaid_nodes", [])),
                "uncovered_intent_concerns": list(parsed.get("uncovered_intent_concerns", [])),
                "redundant_units": list(parsed.get("redundant_units", [])),
            }
        except Exception:
            return {"complete": False, "gaps": [{"description": "Failed to parse coverage analysis", "source": "system", "suggested_task": ""}],
                    "uncovered_mermaid_nodes": [], "uncovered_intent_concerns": [], "redundant_units": []}

    def _llm_refine_units(
        self, llm_router, task_stub: Task, run_dir: Path,
        context_block: str, units: list[dict[str, str]],
        critique: dict[str, object], coverage: dict[str, object],
    ) -> list[dict[str, str]]:
        units_json = json.dumps(units, indent=2)
        problems = json.dumps(critique.get("problems", []), indent=2)
        suggestions = json.dumps(critique.get("suggestions", []), indent=2)
        splits = json.dumps(critique.get("units_needing_split", []), indent=2)
        gaps = json.dumps(coverage.get("gaps", []), indent=2)
        uncovered_nodes = json.dumps(coverage.get("uncovered_mermaid_nodes", []), indent=2)
        uncovered_intents = json.dumps(coverage.get("uncovered_intent_concerns", []), indent=2)
        redundant = json.dumps(coverage.get("redundant_units", []), indent=2)
        prompt = (
            "You are refining atomic implementation units based on TWO review passes:\n"
            "an atomicity critique and a coverage/gap analysis.\n\n"
            "DEFINITION OF ATOMIC:\n"
            "The smallest possible unit of work — one function, one page of code, one reviewable diff.\n"
            "If a unit is too broad, SPLIT it into multiple smaller units rather than making one unit do more.\n\n"
            "You MUST do ALL of the following:\n"
            "1. Fix every PROBLEM from the critique. Apply every SUGGESTION.\n"
            "2. SPLIT every unit listed in units_needing_split into the suggested sub-units.\n"
            "3. ADD new tasks to fill every GAP identified by coverage analysis.\n"
            "4. ADD tasks for every uncovered Mermaid node and uncovered intent concern.\n"
            "5. REMOVE or MERGE redundant units flagged by coverage.\n"
            "6. Each unit must name the exact file, class, and function to modify or create.\n"
            "7. Prefer more smaller units over fewer larger ones.\n\n"
            "Return JSON only: {\"units\": [...]}\n"
            "Same schema: title, objective, rationale, strategy, files_involved\n\n"
            f"Current units:\n{units_json}\n\n"
            "── ATOMICITY CRITIQUE ──\n"
            f"Problems:\n{problems}\n\n"
            f"Suggestions:\n{suggestions}\n\n"
            f"Units needing split:\n{splits}\n\n"
            "── COVERAGE / GAP ANALYSIS ──\n"
            f"Gaps (missing tasks):\n{gaps}\n\n"
            f"Uncovered Mermaid nodes:\n{uncovered_nodes}\n\n"
            f"Uncovered intent concerns:\n{uncovered_intents}\n\n"
            f"Redundant units:\n{redundant}\n\n"
            f"Context:\n{context_block}\n"
        )
        try:
            raw = self._llm_call(llm_router, task_stub, run_dir, prompt)
            refined = self._parse_units_json(raw)
            return refined if refined else units
        except Exception:
            return units

    def _extract_json(self, raw: str) -> dict[str, object]:
        """Extract a JSON object from LLM output, handling markdown fences."""
        text = raw
        if "```" in text:
            text = text.split("```json")[-1].split("```")[0] if "```json" in text else text.split("```")[1].split("```")[0]
        return json.loads(text.strip())

    def _parse_units_json(self, raw: str) -> list[dict[str, str]]:
        parsed = self._extract_json(raw)
        units_raw = list(parsed.get("units") or [])
        units: list[dict[str, str]] = []
        for item in units_raw:
            title = str(item.get("title") or "").strip()
            objective_text = str(item.get("objective") or "").strip()
            if not title or not objective_text:
                continue
            files_involved = item.get("files_involved") or []
            if isinstance(files_involved, list):
                files_str = ", ".join(str(f) for f in files_involved)
            else:
                files_str = str(files_involved)
            full_objective = objective_text
            if files_str:
                full_objective += f"\n\nFiles involved: {files_str}"
            units.append(
                {
                    "title": title,
                    "objective": full_objective,
                    "rationale": str(item.get("rationale") or "").strip(),
                    "strategy": str(item.get("strategy") or "atomic_from_mermaid").strip() or "atomic_from_mermaid",
                }
            )
        return units


    def run_task(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        run = self.ctx.engine.run_once(task.id)
        return {"run": serialize_dataclass(run)}

    def force_promote_objective_review(self, objective_id: str, *, rationale: str, author: str = "operator") -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        reason = rationale.strip()
        if not reason:
            raise ValueError("A rationale is required to force-promote an objective review")
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective.id]
        review = self._promotion_review_for_objective(objective.id, linked_tasks)
        latest_round = (review.get("review_rounds") or [None])[0]
        if not isinstance(latest_round, dict) or not latest_round.get("review_id"):
            raise ValueError("No objective review round exists to override")
        if int(review.get("unresolved_failed_count", 0) or 0) == 0 and bool(review.get("review_clear")):
            return {"objective_id": objective.id, "status": "already_clear"}
        if any(task.status == TaskStatus.ACTIVE for task in linked_tasks):
            raise ValueError("Cannot force-promote while remediation tasks are still active")
        if any(task.status == TaskStatus.PENDING for task in linked_tasks):
            raise ValueError("Cannot force-promote while remediation tasks are still pending")

        review_id = str(latest_round.get("review_id") or "")
        waived_task_ids: list[str] = []
        for task in linked_tasks:
            if task.status != TaskStatus.FAILED:
                continue
            metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
            remediation = metadata.get("objective_review_remediation") if isinstance(metadata.get("objective_review_remediation"), dict) else None
            remediation_review_id = str(remediation.get("review_id") or "") if remediation else ""
            if remediation_review_id and remediation_review_id != review_id:
                continue
            self.task_service.apply_failed_task_disposition(
                task_id=task.id,
                disposition="waive_obsolete",
                rationale=f"Operator force-promoted objective review: {reason}",
            )
            waived_task_ids.append(task.id)

        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="objective_review_override_approved",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="operator",
                content=f"Operator force-approved objective review round {latest_round.get('round_number') or ''}.",
                metadata={
                    "review_id": review_id,
                    "round_number": latest_round.get("round_number"),
                    "rationale": reason,
                    "author": author,
                    "waived_task_ids": waived_task_ids,
                },
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Action receipt: Operator force-approved the latest objective promotion review.",
                metadata={
                    "kind": "objective_review",
                    "status": "force_approved",
                    "review_id": review_id,
                    "rationale": reason,
                    "waived_task_ids": waived_task_ids,
                },
            )
        )
        self.store.update_objective_phase(objective.id)
        return {
            "objective_id": objective.id,
            "status": "force_approved",
            "review_id": review_id,
            "waived_task_ids": waived_task_ids,
        }

    def retry_task(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task.status.value != "failed":
            raise ValueError(f"Task is {task.status.value}, not failed")
        self.store.update_task_status(task_id, TaskStatus.PENDING)
        engine = getattr(self.ctx, "engine", None)
        if engine is not None:
            _BACKGROUND_SUPERVISOR.start(task.project_id, engine, watch=True)
        return {"task_id": task_id, "status": "pending"}

    def _auto_retry_restart_safe_failed_task(self, task: Task) -> bool:
        if task.status != TaskStatus.FAILED:
            return False
        runs = self.store.list_runs(task.id)
        if not runs:
            return False
        latest_run = runs[-1]
        metadata = dict(task.external_ref_metadata) if isinstance(task.external_ref_metadata, dict) else {}
        triage = metadata.get("auto_restart_triage") if isinstance(metadata.get("auto_restart_triage"), dict) else {}
        if str(triage.get("source_run_id") or "") == latest_run.id:
            return False

        reason = ""
        if latest_run.summary == "Recovered: process crash detected" and latest_run.attempt < task.max_attempts:
            reason = "recovered_process_crash"
        else:
            evaluations = self.store.list_evaluations(latest_run.id)
            latest_evaluation = evaluations[-1] if evaluations else None
            details = latest_evaluation.details if latest_evaluation is not None and isinstance(latest_evaluation.details, dict) else {}
            diagnostics = details.get("diagnostics") if isinstance(details.get("diagnostics"), dict) else {}
            failure_category = str(diagnostics.get("failure_category") or "").strip()
            infrastructure_failure = bool(diagnostics.get("infrastructure_failure"))
            restart_safe_categories = {"executor_process_failure", "executor_timeout", "llm_executor_failure", "workspace_contract_failure"}
            if infrastructure_failure and failure_category in restart_safe_categories and latest_run.attempt < task.max_attempts:
                reason = failure_category

        if not reason:
            return False

        metadata["auto_restart_triage"] = {
            "disposition": "retry_as_is",
            "reason": reason,
            "source_run_id": latest_run.id,
            "source_attempt": latest_run.attempt,
            "requeued_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        self.store.update_task_external_metadata(task.id, metadata)
        self.store.update_task_status(task.id, TaskStatus.PENDING)
        if task.objective_id:
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="action_receipt",
                    project_id=task.project_id,
                    objective_id=task.objective_id,
                    task_id=task.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Action receipt: Automatically requeued restart-safe failed task {task.title}.",
                    metadata={"kind": "failed_task_auto_requeued", "task_id": task.id, "source_run_id": latest_run.id, "reason": reason},
                )
            )
        engine = getattr(self.ctx, "engine", None)
        if engine is not None:
            _BACKGROUND_SUPERVISOR.start(task.project_id, engine, watch=True)
        return True

    def retry_all_failed(self, project_id: str) -> dict[str, object]:
        # Check LLM availability via the central gate before requeuing.
        gate = self.ctx.engine.llm_gate
        gate.reset()  # Force a fresh probe.
        if not gate.is_available():
            raise ValueError(f"No LLM backends available. Probes: {gate.last_probe_results}")

        tasks = self.store.list_tasks(project_id=project_id)
        reset_count = 0
        for task in tasks:
            if task.status == TaskStatus.FAILED:
                self.store.update_task_status(task.id, TaskStatus.PENDING)
                reset_count += 1
        engine = getattr(self.ctx, "engine", None)
        if reset_count > 0 and engine is not None:
            _BACKGROUND_SUPERVISOR.start(project_id, engine, watch=True)
        return {"reset_count": reset_count, "probe_results": gate.last_probe_results}

    def start_supervisor(self, project_id: str) -> dict[str, object]:
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        started = _BACKGROUND_SUPERVISOR.start(project_id, self.ctx.engine, watch=True)
        return {
            "started": started,
            "supervisor": _BACKGROUND_SUPERVISOR.status(project_id),
        }

    def stop_supervisor(self, project_id: str) -> dict[str, object]:
        stopped = _BACKGROUND_SUPERVISOR.stop(project_id)
        return {
            "stopped": stopped,
            "supervisor": _BACKGROUND_SUPERVISOR.status(project_id),
        }

    def supervisor_status(self, project_id: str) -> dict[str, object]:
        return {
            "running": _BACKGROUND_SUPERVISOR.is_running(project_id),
            "supervisor": _BACKGROUND_SUPERVISOR.status(project_id),
        }

    def harness_overview(self) -> dict[str, object]:
        """System-wide harness dashboard data."""
        projects = []
        global_counts = {"completed": 0, "active": 0, "failed": 0, "pending": 0}
        for project in self.store.list_projects():
            metrics = self.store.metrics_snapshot(project.id)
            tasks_by_status = metrics.get("tasks_by_status", {})
            for status_key in global_counts:
                global_counts[status_key] += int(tasks_by_status.get(status_key, 0))
            objectives = self.store.list_objectives(project.id)
            active_objective = None
            for obj in objectives:
                if obj.status.value in ("executing", "planning"):
                    gen = self._atomic_generation_state(obj.id)
                    linked_tasks = [t for t in self.store.list_tasks(project.id) if t.objective_id == obj.id]
                    task_counts = {"completed": 0, "active": 0, "failed": 0, "pending": 0}
                    for t in linked_tasks:
                        s = t.status.value if hasattr(t.status, "value") else str(t.status)
                        if s in task_counts:
                            task_counts[s] += 1
                    active_objective = {
                        "id": obj.id,
                        "title": obj.title,
                        "status": obj.status.value,
                        "atomic_generation": gen,
                        "task_counts": task_counts,
                        "task_total": len(linked_tasks),
                    }
                    break
            supervisor = _BACKGROUND_SUPERVISOR.status(project.id)
            projects.append({
                "id": project.id,
                "name": project.name,
                "supervisor": {
                    "running": _BACKGROUND_SUPERVISOR.is_running(project.id),
                    **supervisor,
                },
                "tasks_by_status": dict(tasks_by_status),
                "task_total": sum(int(v) for v in tasks_by_status.values()),
                "active_objective": active_objective,
            })
        # LLM health from router
        llm_health = []
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is not None:
            for name in sorted(llm_router.executors.keys()):
                llm_health.append({
                    "name": name,
                    "demoted": name in llm_router._demoted,
                })
        # Recent events for the feed
        recent_events = []
        for project in self.store.list_projects():
            records = self.store.list_context_records(
                project_id=project.id, record_type="action_receipt",
            )
            for record in records[-20:]:
                text = record.content
                if text.startswith("Action receipt: "):
                    text = text[len("Action receipt: "):]
                recent_events.append({
                    "project_id": project.id,
                    "project_name": project.name,
                    "text": text,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id or "",
                })
            # Also include decomposition telemetry
            telemetry = self.store.list_context_records(
                project_id=project.id, record_type="atomic_decomposition_telemetry",
            )
            for record in telemetry[-10:]:
                recent_events.append({
                    "project_id": project.id,
                    "project_name": project.name,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "objective_id": record.objective_id or "",
                })
        recent_events.sort(key=lambda e: e["created_at"], reverse=True)
        return {
            "global_counts": global_counts,
            "global_total": sum(global_counts.values()),
            "projects": projects,
            "llm_health": llm_health,
            "recent_events": recent_events[:50],
        }

    def run_cli_command(self, command: str) -> dict[str, object]:
        cleaned = command.strip()
        if not cleaned:
            raise ValueError("CLI command must not be empty")
        command_parts = shlex.split(cleaned)
        repo_root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        src_path = str(repo_root / "src")
        existing_pythonpath = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else src_path
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "accruvia_harness",
                "--db",
                str(self.ctx.config.db_path),
                "--workspace",
                str(self.ctx.config.workspace_root),
                *command_parts,
            ],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if stdout and stderr:
            output = f"{stdout}\n\n[stderr]\n{stderr}"
        else:
            output = stdout or stderr or "(no output)"
        return {
            "command": cleaned,
            "exit_code": completed.returncode,
            "output": output,
        }

    def _ensure_first_linked_task(self, objective_id: str) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        if any(task.objective_id == objective.id for task in self.store.list_tasks(objective.project_id)):
            return
        task_payload = self.create_linked_task(objective.id)
        task = task_payload["task"]
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="task_created",
                project_id=objective.project_id,
                objective_id=objective.id,
                task_id=str(task.get("id") or ""),
                visibility="model_visible",
                author_type="system",
                content=f"Created first bounded slice for objective {objective.title}",
                metadata={
                    "task_title": str(task.get("title") or ""),
                    "strategy": str(task.get("strategy") or ""),
                    "generated_from": "intent_and_mermaid",
                },
            )
        )

    def run_cli_output(self, run_id: str) -> dict[str, object]:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"Unknown run: {run_id}")
        sections_raw = self._run_output_sections(run_id)
        sections = [
            {
                "label": section.label,
                "path": section.path,
                "content": section.content,
            }
            for section in sections_raw
        ]
        return {
            "run": serialize_dataclass(run),
            "summary": self._summarize_run_output(run, sections_raw),
            "sections": sections,
        }

    def add_operator_comment(
        self,
        project_ref: str,
        text: str,
        author: str | None,
        objective_id: str | None = None,
    ) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        body = text.strip()
        if not body:
            raise ValueError("Comment text must not be empty")
        if objective_id:
            objective = self.store.get_objective(objective_id)
            if objective is None or objective.project_id != project.id:
                raise ValueError(f"Unknown objective: {objective_id}")
        record = ContextRecord(
            id=new_id("context"),
            record_type="operator_comment",
            project_id=project.id,
            objective_id=objective_id,
            visibility="model_visible",
            author_type="operator",
            author_id=(author or "").strip(),
            content=body,
        )
        self.store.create_context_record(record)
        if objective_id and self._should_auto_complete_interrogation(objective_id):
            self.complete_interrogation_review(objective_id)
        frustration_detected = self._comment_looks_like_frustration(body)
        mermaid_update_requested = False
        if objective_id:
            mermaid_update_requested = self._comment_requests_mermaid_update(
                body,
                project_id=project.id,
                objective_id=objective_id,
            )
        responder_result = self._answer_operator_comment(
            project_id=project.id,
            objective_id=objective_id,
            comment_text=body,
            frustration_detected=frustration_detected,
        )
        proposal = None
        if objective_id and mermaid_update_requested:
            responder_result.reply = (
                responder_result.reply.rstrip()
                + "\n\nGenerating a proposed Mermaid update from your instruction — this will appear shortly."
            )
            responder_result.recommended_action = "review_mermaid"
            _proposal_objective_id = objective_id
            _proposal_project_id = project.id
            _proposal_directive = body

            def _generate_proposal() -> dict[str, object] | None:
                result = self.propose_mermaid_update(_proposal_objective_id, directive=_proposal_directive)
                receipt_content = (
                    "Action receipt: Mermaid proposal generated."
                    if result is not None
                    else "Action receipt: Mermaid update was requested but no proposal was generated."
                )
                receipt_status = "proposal_generated" if result is not None else "not_applied"
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="action_receipt",
                        project_id=_proposal_project_id,
                        objective_id=_proposal_objective_id,
                        visibility="operator_visible",
                        author_type="system",
                        content=receipt_content,
                        metadata={"kind": "mermaid_update", "status": receipt_status},
                    )
                )
                return result

            if getattr(self.ctx, "is_test", False):
                try:
                    proposal = _generate_proposal()
                except Exception:
                    proposal = None
            else:
                # Run Mermaid proposal in background so the text reply returns immediately.
                def _generate_proposal_background() -> None:
                    try:
                        _generate_proposal()
                    except Exception:
                        pass

                threading.Thread(target=_generate_proposal_background, daemon=True).start()
        self._log_ui_memory_retrieval(
            project_id=project.id,
            objective_id=objective_id,
            comment_text=body,
            responder_result=responder_result,
        )
        if frustration_detected:
            triage = triage_frustration(self.store, project_id=project.id, objective_id=objective_id)
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="operator_frustration",
                    project_id=project.id,
                    objective_id=objective_id,
                    visibility="model_visible",
                    author_type="operator",
                    author_id=(author or "").strip(),
                    content=body,
                    metadata={
                        "triage": {
                            "objective_id": triage.objective_id,
                            "likely_causes": triage.likely_causes,
                            "recommendation": triage.recommendation,
                            "confidence": triage.confidence,
                        },
                        "derived_from": "operator_comment",
                    },
                )
            )
            if objective_id:
                self.store.update_objective_status(objective_id, ObjectiveStatus.INVESTIGATING)
        reply_record = ContextRecord(
            id=new_id("context"),
            record_type="harness_reply",
            project_id=project.id,
            objective_id=objective_id,
            visibility="operator_visible",
            author_type="system",
            content=responder_result.reply,
            metadata={
                "reply_to": record.id,
                "recommended_action": responder_result.recommended_action,
                "evidence_refs": responder_result.evidence_refs,
                "mode_shift": responder_result.mode_shift,
                "retrieved_memories": [serialize_dataclass(memory) for memory in responder_result.retrieved_memories],
                "llm_backend": responder_result.llm_backend,
                "prompt_path": responder_result.prompt_path,
                "response_path": responder_result.response_path,
            },
        )
        self.store.create_context_record(reply_record)
        return {
            "comment": {
                "id": record.id,
                "author": record.author_id,
                "text": record.content,
                "objective_id": record.objective_id,
                "created_at": record.created_at.isoformat(),
            },
            "reply": {
                "id": reply_record.id,
                "text": reply_record.content,
                "objective_id": reply_record.objective_id,
                "created_at": reply_record.created_at.isoformat(),
                "recommended_action": responder_result.recommended_action,
                "evidence_refs": responder_result.evidence_refs,
                "mode_shift": responder_result.mode_shift,
                "retrieved_memories": [serialize_dataclass(memory) for memory in responder_result.retrieved_memories],
                "llm_backend": responder_result.llm_backend,
                "prompt_path": responder_result.prompt_path,
                "response_path": responder_result.response_path,
            },
            "frustration_detected": frustration_detected,
            "mermaid_proposal": proposal,
        }

    def add_operator_frustration(
        self,
        project_ref: str,
        text: str,
        author: str | None,
        objective_id: str | None = None,
    ) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        body = text.strip()
        if not body:
            raise ValueError("Frustration text must not be empty")
        if objective_id:
            objective = self.store.get_objective(objective_id)
            if objective is None or objective.project_id != project.id:
                raise ValueError(f"Unknown objective: {objective_id}")
        triage = triage_frustration(self.store, project_id=project.id, objective_id=objective_id)
        record = ContextRecord(
            id=new_id("context"),
            record_type="operator_frustration",
            project_id=project.id,
            objective_id=objective_id,
            visibility="model_visible",
            author_type="operator",
            author_id=(author or "").strip(),
            content=body,
            metadata={
                "triage": {
                    "objective_id": triage.objective_id,
                    "likely_causes": triage.likely_causes,
                    "recommendation": triage.recommendation,
                    "confidence": triage.confidence,
                }
            },
        )
        self.store.create_context_record(record)
        if objective_id:
            self.store.update_objective_status(objective_id, ObjectiveStatus.INVESTIGATING)
        return {
            "frustration": {
                "id": record.id,
                "author": record.author_id,
                "text": record.content,
                "objective_id": record.objective_id,
                "created_at": record.created_at.isoformat(),
                "triage": record.metadata["triage"],
            }
        }

    def _operator_comments(self, project_id: str) -> list[dict[str, object]]:
        comments = []
        for record in self.store.list_context_records(project_id=project_id, record_type="operator_comment"):
            comments.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "author": record.author_id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                }
            )
        return comments

    def _operator_frustrations(self, project_id: str) -> list[dict[str, object]]:
        frustrations = []
        for record in self.store.list_context_records(project_id=project_id, record_type="operator_frustration"):
            triage = record.metadata.get("triage", {})
            frustrations.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "author": record.author_id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "triage": triage,
                }
            )
        return frustrations

    def _action_receipts(self, project_id: str) -> list[dict[str, object]]:
        receipts = []
        for record in self.store.list_context_records(project_id=project_id, record_type="action_receipt"):
            text = record.content
            if text.startswith("Action receipt: "):
                text = text[len("Action receipt: "):]
            receipts.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "text": text,
                    "created_at": record.created_at.isoformat(),
                    "metadata": record.metadata,
                }
            )
        return receipts

    def _harness_replies(self, project_id: str) -> list[dict[str, object]]:
        replies = []
        for record in self.store.list_context_records(project_id=project_id, record_type="harness_reply"):
            replies.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "text": record.content,
                    "created_at": record.created_at.isoformat(),
                    "recommended_action": record.metadata.get("recommended_action", "none"),
                    "evidence_refs": record.metadata.get("evidence_refs", []),
                    "mode_shift": record.metadata.get("mode_shift", "none"),
                    "retrieved_memories": record.metadata.get("retrieved_memories", []),
                    "llm_backend": record.metadata.get("llm_backend", ""),
                    "prompt_path": record.metadata.get("prompt_path", ""),
                    "response_path": record.metadata.get("response_path", ""),
                }
            )
        return replies

    def _answer_operator_comment(
        self,
        *,
        project_id: str,
        objective_id: str | None,
        comment_text: str,
        frustration_detected: bool,
    ) -> ResponderResult:
        packet = self._build_responder_context_packet(
            project_id=project_id,
            objective_id=objective_id,
            comment_text=comment_text,
            frustration_detected=frustration_detected,
        )
        llm_result = self._answer_operator_comment_with_llm(
            packet=packet,
            project_id=project_id,
            objective_id=objective_id,
            comment_text=comment_text,
        )
        if llm_result is not None:
            return llm_result
        return answer_ui_message(packet, comment_text)

    def _answer_operator_comment_with_llm(
        self,
        *,
        packet: ResponderContextPacket,
        project_id: str,
        objective_id: str | None,
        comment_text: str,
    ) -> ResponderResult | None:
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is None or not getattr(llm_router, "executors", {}):
            return None
        prompt = self._build_ui_responder_prompt(
            packet=packet,
            project_id=project_id,
            objective_id=objective_id,
            comment_text=comment_text,
        )
        run_dir = self.workspace_root / "ui_responder" / (objective_id or project_id) / new_id("reply")
        run_dir.mkdir(parents=True, exist_ok=True)
        task = Task(
            id=new_id("ui_reply_task"),
            project_id=project_id,
            title=f"UI response for {packet.objective.title if packet.objective else packet.project_name}",
            objective="Answer the operator directly from current harness state and full available context.",
            strategy="ui_responder",
            status=TaskStatus.COMPLETED,
        )
        run = Run(
            id=new_id("ui_reply_run"),
            task_id=task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary=f"UI reply for {objective_id or project_id}",
        )
        try:
            result, backend = llm_router.execute(
                LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir)
            )
        except LLMExecutionError:
            return None
        parsed = self._parse_ui_responder_response(result.response_text)
        if parsed is None:
            return None
        return ResponderResult(
            reply=parsed["reply"],
            recommended_action=parsed["recommended_action"],
            evidence_refs=parsed["evidence_refs"],
            mode_shift=parsed["mode_shift"],
            retrieved_memories=packet.retrieved_memories,
            llm_backend=backend,
            prompt_path=str(result.prompt_path),
            response_path=str(result.response_path),
        )

    def _interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        deterministic = self._deterministic_interrogation_review(objective_id)
        completions = self.store.list_context_records(objective_id=objective_id, record_type="interrogation_completed")
        latest_completion = completions[-1] if completions else None
        if latest_completion is not None:
            return self._recorded_interrogation_review(latest_completion, completed=True)

        drafts = self.store.list_context_records(objective_id=objective_id, record_type="interrogation_draft")
        latest_draft = drafts[-1] if drafts else None
        if latest_draft is not None:
            return self._recorded_interrogation_review(latest_draft, completed=False)

        if deterministic["plan_elements"]:
            generated = self._generate_interrogation_review(objective_id)
            if generated.get("generated_by") != "deterministic":
                self._persist_interrogation_record("interrogation_draft", objective, generated)
                drafts = self.store.list_context_records(objective_id=objective_id, record_type="interrogation_draft")
                latest_draft = drafts[-1] if drafts else None
                if latest_draft is not None:
                    return self._recorded_interrogation_review(latest_draft, completed=False)
        return deterministic

    def _generate_interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        deterministic = self._deterministic_interrogation_review(objective_id)
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is None:
            return deterministic

        prompt = self._build_interrogation_prompt(objective_id, deterministic)
        run_dir = self.workspace_root / "interrogation" / "objective" / objective_id / new_id("redteam")
        run_dir.mkdir(parents=True, exist_ok=True)
        task = Task(
            id=new_id("interrogation_task"),
            project_id=objective.project_id,
            title=f"Interrogate objective {objective.title}",
            objective="Interrogate and red-team the objective before Mermaid review.",
            status=TaskStatus.COMPLETED,
        )
        run = Run(
            id=new_id("interrogation_run"),
            task_id=task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary=f"LLM red-team for objective {objective.id}",
        )
        try:
            result, backend = llm_router.execute(LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir))
        except LLMExecutionError:
            return deterministic

        parsed = self._parse_interrogation_response(result.response_text)
        if parsed is None:
            return deterministic
        return {
            "completed": False,
            "summary": parsed["summary"],
            "plan_elements": parsed["plan_elements"],
            "questions": parsed["questions"],
            "generated_by": "llm",
            "backend": backend,
            "prompt_path": str(result.prompt_path),
            "response_path": str(result.response_path),
        }

    def _deterministic_interrogation_review(self, objective_id: str) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        intent_model = self.store.latest_intent_model(objective_id)
        desired_outcome = (intent_model.intent_summary if intent_model is not None else "").strip()
        success_definition = (intent_model.success_definition if intent_model is not None else "").strip()
        non_negotiables = list(intent_model.non_negotiables) if intent_model is not None else []

        plan_elements: list[str] = []
        if desired_outcome:
            plan_elements.append(f"Desired outcome: {desired_outcome}")
        if success_definition:
            plan_elements.append(f"Success definition: {success_definition}")
        if non_negotiables:
            plan_elements.append("Non-negotiables: " + ", ".join(non_negotiables[:4]))

        questions: list[str] = []
        if desired_outcome:
            questions.append("What concrete operator experience should feel different if this objective succeeds?")
        else:
            questions.append("What exact outcome should exist before the harness starts planning?")
        if success_definition:
            questions.append("What evidence would prove this objective is complete instead of only improved?")
        else:
            questions.append("How should the harness measure success for this objective?")
        questions.append("What is the most likely way the current plan could still miss your intent?")
        questions.append("What ambiguity should be resolved before Mermaid review?")
        return {
            "completed": False,
            "summary": "The harness should interrogate the objective and self-red-team the plan before Mermaid review.",
            "plan_elements": plan_elements,
            "questions": questions,
            "generated_by": "deterministic",
            "backend": None,
        }

    def _recorded_interrogation_review(self, record: ContextRecord, *, completed: bool) -> dict[str, object]:
        return {
            "completed": completed,
            "summary": record.content,
            "plan_elements": list(record.metadata.get("plan_elements") or []),
            "questions": list(record.metadata.get("questions") or []),
            "generated_by": record.metadata.get("generated_by", "deterministic"),
            "backend": record.metadata.get("backend"),
        }

    def _persist_interrogation_record(self, record_type: str, objective: Objective, review: dict[str, object]) -> None:
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type=record_type,
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="system",
                content=str(review["summary"]),
                metadata={
                    "plan_elements": review["plan_elements"],
                    "questions": review["questions"],
                    "generated_by": review.get("generated_by", "deterministic"),
                    "backend": review.get("backend"),
                    "prompt_path": review.get("prompt_path"),
                    "response_path": review.get("response_path"),
                },
            )
        )

    def _should_auto_complete_interrogation(self, objective_id: str) -> bool:
        review = self._interrogation_review(objective_id)
        if review.get("completed"):
            return False
        questions = list(review.get("questions") or [])
        if not questions:
            return False
        intent_model = self.store.latest_intent_model(objective_id)
        created_at = intent_model.created_at.isoformat() if intent_model is not None else ""
        answers = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")
            if not created_at or record.created_at.isoformat() >= created_at
        ]
        return len(answers) >= len(questions)

    def _build_interrogation_prompt(self, objective_id: str, deterministic: dict[str, object]) -> str:
        objective = self.store.get_objective(objective_id)
        intent_model = self.store.latest_intent_model(objective_id)
        comments = self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")[-6:]
        return (
            "You are red-teaming a software objective before process review.\n"
            "Your job is to interrogate the objective, extract the likely plan elements, and list the sharpest unresolved questions.\n"
            "Return JSON only with keys: summary, plan_elements, questions.\n"
            "summary: short paragraph\n"
            "plan_elements: array of concise strings\n"
            "questions: array of concise red-team questions\n\n"
            f"Objective title: {objective.title if objective else ''}\n"
            f"Objective summary: {objective.summary if objective else ''}\n"
            f"Intent summary: {intent_model.intent_summary if intent_model else ''}\n"
            f"Success definition: {intent_model.success_definition if intent_model else ''}\n"
            f"Non-negotiables: {json.dumps(intent_model.non_negotiables if intent_model else [])}\n"
            f"Recent operator comments: {json.dumps([record.content for record in comments], indent=2)}\n"
            f"Current deterministic review: {json.dumps(deterministic, indent=2, sort_keys=True)}\n"
        )

    def _parse_interrogation_response(self, text: str) -> dict[str, object] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        candidates.extend(fenced)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            summary = str(payload.get("summary") or "").strip()
            plan_elements = [str(item).strip() for item in list(payload.get("plan_elements") or []) if str(item).strip()]
            questions = [str(item).strip() for item in list(payload.get("questions") or []) if str(item).strip()]
            if summary and plan_elements and questions:
                return {
                    "summary": summary,
                    "plan_elements": plan_elements,
                    "questions": questions,
                }
        return None

    def _build_objective_review_prompt(
        self,
        objective: Objective,
        objective_payload: dict[str, object],
        linked_tasks: list[Task],
    ) -> str:
        intent_model = self.store.latest_intent_model(objective.id)
        tasks_payload = [
            {
                "title": task.title,
                "status": task.status.value,
                "objective": task.objective,
                "strategy": task.strategy,
                "metadata": task.external_ref_metadata,
            }
            for task in linked_tasks
        ]
        prior_rounds = []
        for round_row in list(objective_payload.get("review_rounds") or [])[:3]:
            if not isinstance(round_row, dict):
                continue
            prior_rounds.append(
                {
                    "round_number": round_row.get("round_number"),
                    "status": round_row.get("status"),
                    "verdict_counts": round_row.get("verdict_counts"),
                    "remediation_counts": round_row.get("remediation_counts"),
                    "review_cycle_artifact": round_row.get("review_cycle_artifact"),
                    "worker_responses": round_row.get("worker_responses"),
                    "reviewer_rebuttals": round_row.get("reviewer_rebuttals"),
                    "packets": [
                        {
                            "dimension": packet.get("dimension"),
                            "verdict": packet.get("verdict"),
                            "progress_status": packet.get("progress_status"),
                            "summary": packet.get("summary"),
                            "evidence_contract": packet.get("evidence_contract"),
                        }
                        for packet in list(round_row.get("packets") or [])
                    ],
                }
            )
        return (
            "You are the objective-level promotion review board for the accruvia harness.\n"
            "Review the objective as a whole after execution completed.\n"
            "You may be reviewing a later round after remediation from prior rounds.\n"
            "Judge progress against previous rounds instead of repeating the same concern blindly.\n"
            "Every non-pass packet becomes an Evidence Contract for remediation. Review findings and remediation must speak the same artifact type.\n"
            "Do not treat an actively running review/remediation cycle as proof of failure on its own.\n"
            "If the current lifecycle is still in progress, distinguish missing implementation from missing final evidence.\n"
            "Return JSON only with keys: summary, packets.\n"
            "packets must be an array. Each packet must contain reviewer, dimension, verdict, progress_status, severity, owner_scope, summary, findings, evidence, required_artifact_type, artifact_schema, closure_criteria, evidence_required.\n"
            "reviewer: short reviewer name\n"
            "dimension: one of intent_fidelity, unit_test_coverage, integration_e2e_coverage, security, devops, atomic_fidelity, code_structure\n"
            "verdict: one of pass, concern, remediation_required\n"
            "progress_status: one of new_concern, still_blocking, improving, resolved, not_applicable\n"
            "severity: one of low, medium, high\n"
            "owner_scope: short concrete owner scope such as objective review orchestration, integration tests, promotion apply-back, ui workflow\n"
            "summary: short paragraph\n"
            "findings: array of short strings\n"
            "evidence: array of short strings\n"
            "required_artifact_type: REQUIRED for concern and remediation_required. Name the exact artifact type that must be produced.\n"
            "artifact_schema: REQUIRED for concern and remediation_required. JSON object with at least type, description, and required_fields.\n"
            "closure_criteria: REQUIRED for concern and remediation_required. Must be concrete and measurable.\n"
            "evidence_required: REQUIRED for concern and remediation_required. Must name the artifact or proof required to clear the finding.\n"
            "repeat_reason: REQUIRED when verdict is concern or remediation_required and progress_status is improving, still_blocking, or resolved.\n"
            "Reject vague language. Do not say 'improve testing' or 'more evidence' without a measurable closure target.\n\n"
            f"Objective title: {objective.title}\n"
            f"Objective summary: {objective.summary}\n"
            f"Objective status: {objective.status.value}\n"
            f"Intent summary: {intent_model.intent_summary if intent_model else ''}\n"
            f"Success definition: {intent_model.success_definition if intent_model else ''}\n"
            f"Objective review summary: {json.dumps(objective_payload, indent=2, sort_keys=True)}\n"
            f"Previous review rounds: {json.dumps(prior_rounds, indent=2, sort_keys=True)}\n"
            f"Linked tasks: {json.dumps(tasks_payload, indent=2, sort_keys=True)}\n"
        )

    def _parse_objective_review_response(
        self,
        text: str,
        *,
        objective_payload: dict[str, object] | None = None,
    ) -> list[dict[str, object]] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        candidates.extend(fenced)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            packets = payload.get("packets")
            if not isinstance(packets, list):
                continue
            parsed: list[dict[str, object]] = []
            for item in packets:
                if not isinstance(item, dict):
                    continue
                validated = self._validate_objective_review_packet(item, objective_payload=objective_payload)
                if validated is not None:
                    parsed.append(validated)
            if parsed:
                return parsed
        return None

    def _validate_objective_review_packet(
        self,
        item: dict[str, object],
        *,
        objective_payload: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        reviewer = str(item.get("reviewer") or "").strip()
        dimension = str(item.get("dimension") or "").strip()
        verdict = str(item.get("verdict") or "").strip()
        progress_status = str(item.get("progress_status") or "not_applicable").strip() or "not_applicable"
        summary = str(item.get("summary") or "").strip()
        findings = [str(v).strip() for v in list(item.get("findings") or []) if str(v).strip()]
        evidence = [str(v).strip() for v in list(item.get("evidence") or []) if str(v).strip()]
        severity = str(item.get("severity") or "").strip().lower()
        owner_scope = str(item.get("owner_scope") or "").strip()
        contract_payload = item.get("evidence_contract") if isinstance(item.get("evidence_contract"), dict) else {}
        required_artifact_type = str(
            item.get("required_artifact_type") or contract_payload.get("required_artifact_type") or ""
        ).strip()
        artifact_schema = self._normalize_objective_review_artifact_schema(
            item.get("artifact_schema") if item.get("artifact_schema") is not None else contract_payload.get("artifact_schema"),
            required_artifact_type=required_artifact_type,
            dimension=dimension,
        )
        closure_criteria = str(item.get("closure_criteria") or contract_payload.get("closure_criteria") or "").strip()
        evidence_required = str(item.get("evidence_required") or contract_payload.get("evidence_required") or "").strip()
        repeat_reason = str(item.get("repeat_reason") or "").strip()
        if not reviewer or not summary:
            return None
        if dimension not in _OBJECTIVE_REVIEW_DIMENSIONS:
            return None
        if verdict not in _OBJECTIVE_REVIEW_VERDICTS:
            return None
        if progress_status not in _OBJECTIVE_REVIEW_PROGRESS:
            return None
        if verdict == "pass":
            return {
                "reviewer": reviewer,
                "dimension": dimension,
                "verdict": verdict,
                "progress_status": progress_status,
                "severity": "",
                "owner_scope": "",
                "summary": summary,
                "findings": findings,
                "evidence": evidence,
                "required_artifact_type": "",
                "artifact_schema": {},
                "evidence_contract": {},
                "closure_criteria": "",
                "evidence_required": "",
                "repeat_reason": repeat_reason,
            }
        if severity not in _OBJECTIVE_REVIEW_SEVERITIES:
            return None
        if not owner_scope or not closure_criteria or not evidence_required or not required_artifact_type or artifact_schema is None:
            return None
        if progress_status in {"improving", "still_blocking", "resolved"} and not repeat_reason:
            return None
        if not findings or not evidence:
            return None
        lowered_closure = closure_criteria.lower()
        lowered_evidence_required = evidence_required.lower()
        if not any(
            marker in lowered_closure
            for marker in ("must", "shows", "show", "recorded", "exists", "complete", "passes", "pass", "zero", "all ", "at least", "no ")
        ):
            return None
        if any(phrase in lowered_closure for phrase in _OBJECTIVE_REVIEW_VAGUE_PHRASES):
            return None
        if any(phrase in lowered_evidence_required for phrase in ("more evidence", "stronger evidence", "better tests", "improve")):
            return None
        if (
            objective_payload
            and progress_status in {"improving", "still_blocking", "resolved"}
            and self._objective_round_artifact_is_present(objective_payload)
            and self._packet_requests_round_artifact(evidence_required, closure_criteria)
        ):
            return None
        evidence_contract = {
            "required_artifact_type": required_artifact_type,
            "artifact_schema": artifact_schema,
            "closure_criteria": closure_criteria,
            "evidence_required": evidence_required,
        }
        return {
            "reviewer": reviewer,
            "dimension": dimension,
            "verdict": verdict,
            "progress_status": progress_status,
            "severity": severity,
            "owner_scope": owner_scope,
            "summary": summary,
            "findings": findings,
            "evidence": evidence,
            "required_artifact_type": required_artifact_type,
            "artifact_schema": artifact_schema,
            "evidence_contract": evidence_contract,
            "closure_criteria": closure_criteria,
            "evidence_required": evidence_required,
            "repeat_reason": repeat_reason,
        }

    def _objective_round_artifact_is_present(self, objective_payload: dict[str, object]) -> bool:
        rounds = list(objective_payload.get("review_rounds") or [])
        if not rounds:
            return False
        latest = rounds[0] if isinstance(rounds[0], dict) else {}
        if not latest:
            return False
        cycle_artifact = latest.get("review_cycle_artifact") if isinstance(latest.get("review_cycle_artifact"), dict) else {}
        if cycle_artifact:
            return bool(cycle_artifact.get("record_id")) and bool(cycle_artifact.get("terminal_event"))
        packet_count = int(latest.get("packet_count") or 0)
        completed_at = str(latest.get("completed_at") or "")
        verdict_counts = latest.get("verdict_counts") if isinstance(latest.get("verdict_counts"), dict) else {}
        remediation_counts = latest.get("remediation_counts") if isinstance(latest.get("remediation_counts"), dict) else {}
        terminal_branch_present = (
            str(latest.get("status") or "") == "passed"
            or int(remediation_counts.get("total", 0) or 0) > 0
        )
        return bool(completed_at) and packet_count >= 7 and sum(int(verdict_counts.get(k, 0) or 0) for k in ("pass", "concern", "remediation_required")) > 0 and terminal_branch_present

    def _packet_requests_round_artifact(self, evidence_required: str, closure_criteria: str) -> bool:
        text = f"{evidence_required}\n{closure_criteria}".lower()
        markers = (
            "completed objective review",
            "completed objective review run artifact",
            "persisted objective review artifact",
            "completed end-to-end objective review",
            "completed objective review cycle",
            "completed round",
            "terminal round state",
            "completed_at",
            "persisted reviewer packets",
            "review start",
            "terminal review",
            "review approval",
            "remediation linkage",
        )
        return any(marker in text for marker in markers)

    def _normalize_objective_review_artifact_schema(
        self,
        raw_schema: object,
        *,
        required_artifact_type: str,
        dimension: str,
    ) -> dict[str, object] | None:
        artifact_type = required_artifact_type.strip()
        if not artifact_type:
            return None
        schema: dict[str, object] = {}
        if isinstance(raw_schema, dict):
            schema = dict(raw_schema)
        elif isinstance(raw_schema, str) and raw_schema.strip():
            schema = {"description": raw_schema.strip()}
        required_fields = [str(item).strip() for item in list(schema.get("required_fields") or []) if str(item).strip()]
        if not required_fields:
            required_fields = self._default_review_artifact_required_fields(artifact_type)
        description = str(schema.get("description") or "").strip()
        if not description:
            description = f"Persist one {artifact_type} artifact for the {dimension or 'objective review'} dimension."
        normalized = {
            "type": str(schema.get("type") or artifact_type).strip() or artifact_type,
            "description": description,
            "required_fields": required_fields,
        }
        if schema.get("record_locator"):
            normalized["record_locator"] = schema.get("record_locator")
        return normalized

    def _default_review_artifact_required_fields(self, artifact_type: str) -> list[str]:
        lowered = artifact_type.lower()
        if "review_cycle" in lowered or "telemetry" in lowered:
            return ["review_id", "start_event", "packet_persistence_events", "terminal_event", "linked_outcome"]
        if "review_packet" in lowered:
            return ["review_id", "reviewer", "dimension", "verdict", "artifacts"]
        if "test" in lowered:
            return ["artifact_path", "test_targets", "result"]
        return ["artifact_path", "summary"]

    def _deterministic_objective_review_packets(self, objective_payload: dict[str, object]) -> list[dict[str, object]]:
        counts = objective_payload.get("task_counts", {}) if isinstance(objective_payload, dict) else {}
        failed = int(counts.get("failed", 0) or 0)
        waived = int(objective_payload.get("waived_failed_count", 0) or 0)
        unresolved = int(objective_payload.get("unresolved_failed_count", 0) or 0)
        packets = [
            {
                "reviewer": "Intent agent",
                "dimension": "intent_fidelity",
                "verdict": "pass" if unresolved == 0 else "concern",
                "progress_status": "not_applicable",
                "severity": "" if unresolved == 0 else "medium",
                "owner_scope": "" if unresolved == 0 else "failed task governance",
                "summary": "Execution completed and the objective reached a resolved state. Review the linked task outcomes against the original intent before promotion.",
                "findings": [] if unresolved == 0 else ["There are unresolved failed tasks that still need explicit disposition."],
                "evidence": [f"Completed tasks: {int(counts.get('completed', 0) or 0)}", f"Unresolved failed tasks: {unresolved}"],
                "required_artifact_type": "" if unresolved == 0 else "failed_task_disposition_record",
                "artifact_schema": {} if unresolved == 0 else {
                    "type": "failed_task_disposition_record",
                    "description": "Each unresolved failed task must carry an explicit persisted disposition before promotion.",
                    "required_fields": ["task_id", "disposition", "rationale"],
                },
                "evidence_contract": {} if unresolved == 0 else {
                    "required_artifact_type": "failed_task_disposition_record",
                    "artifact_schema": {
                        "type": "failed_task_disposition_record",
                        "description": "Each unresolved failed task must carry an explicit persisted disposition before promotion.",
                        "required_fields": ["task_id", "disposition", "rationale"],
                    },
                    "closure_criteria": "All failed tasks for the objective must be explicitly waived, superseded, or resolved so unresolved failed task count is zero.",
                    "evidence_required": "Objective summary shows zero unresolved failed tasks and records explicit failed-task dispositions.",
                },
                "closure_criteria": "" if unresolved == 0 else "All failed tasks for the objective must be explicitly waived, superseded, or resolved so unresolved failed task count is zero.",
                "evidence_required": "" if unresolved == 0 else "Objective summary shows zero unresolved failed tasks and records explicit failed-task dispositions.",
                "repeat_reason": "",
                "llm_usage": {"shared_invocation": True, "reported": False, "missing_reason": "deterministic_review_packet"},
                "llm_usage_reported": False,
                "llm_usage_source": "deterministic",
            },
            {
                "reviewer": "QA agent",
                "dimension": "unit_test_coverage",
                "verdict": "concern",
                "progress_status": "new_concern",
                "severity": "medium",
                "owner_scope": "objective review evidence",
                "summary": "Unit and integration evidence should be reviewed from the completed task reports before promotion.",
                "findings": ["Objective-level QA packets are not yet derived from report artifacts."],
                "evidence": [f"Historical failed tasks: {failed}", f"Waived failed tasks: {waived}"],
                "required_artifact_type": "objective_review_packet",
                "artifact_schema": {
                    "type": "objective_review_packet",
                    "description": "QA closure requires a persisted review packet that cites the exact completed-task test artifacts.",
                    "required_fields": ["review_id", "reviewer", "dimension", "verdict", "artifacts"],
                },
                "evidence_contract": {
                    "required_artifact_type": "objective_review_packet",
                    "artifact_schema": {
                        "type": "objective_review_packet",
                        "description": "QA closure requires a persisted review packet that cites the exact completed-task test artifacts.",
                        "required_fields": ["review_id", "reviewer", "dimension", "verdict", "artifacts"],
                    },
                    "closure_criteria": "Objective review packets must cite concrete unit-test or integration-test evidence from completed task artifacts for the QA dimensions.",
                    "evidence_required": "A recorded review packet that references completed-task test artifacts and concludes QA pass or resolved concern status.",
                },
                "closure_criteria": "Objective review packets must cite concrete unit-test or integration-test evidence from completed task artifacts for the QA dimensions.",
                "evidence_required": "A recorded review packet that references completed-task test artifacts and concludes QA pass or resolved concern status.",
                "repeat_reason": "",
                "llm_usage": {"shared_invocation": True, "reported": False, "missing_reason": "deterministic_review_packet"},
                "llm_usage_reported": False,
                "llm_usage_source": "deterministic",
            },
            {
                "reviewer": "Structure agent",
                "dimension": "code_structure",
                "verdict": "concern" if waived else "pass",
                "progress_status": "new_concern" if waived else "not_applicable",
                "severity": "medium" if waived else "",
                "owner_scope": "code structure" if waived else "",
                "summary": "Historical control-plane failures were waived, so code structure should be reviewed carefully before promotion.",
                "findings": ["Waived control-plane failures deserve a human review pass."] if waived else [],
                "evidence": [f"Waived failed tasks: {waived}"],
                "required_artifact_type": "" if not waived else "failed_task_disposition_record",
                "artifact_schema": {} if not waived else {
                    "type": "failed_task_disposition_record",
                    "description": "Waived failed tasks must retain persisted superseding or waiver rationale.",
                    "required_fields": ["task_id", "disposition", "rationale"],
                },
                "evidence_contract": {} if not waived else {
                    "required_artifact_type": "failed_task_disposition_record",
                    "artifact_schema": {
                        "type": "failed_task_disposition_record",
                        "description": "Waived failed tasks must retain persisted superseding or waiver rationale.",
                        "required_fields": ["task_id", "disposition", "rationale"],
                    },
                    "closure_criteria": "Historical failed tasks must be superseded or waived with rationale so fragmented partial work is not left unresolved.",
                    "evidence_required": "Failed-task records show explicit superseding or waiver rationale for every historical failure.",
                },
                "closure_criteria": "Historical failed tasks must be superseded or waived with rationale so fragmented partial work is not left unresolved." if waived else "",
                "evidence_required": "Failed-task records show explicit superseding or waiver rationale for every historical failure." if waived else "",
                "repeat_reason": "",
                "llm_usage": {"shared_invocation": True, "reported": False, "missing_reason": "deterministic_review_packet"},
                "llm_usage_reported": False,
                "llm_usage_source": "deterministic",
            },
        ]
        return packets

    def _build_ui_responder_prompt(
        self,
        *,
        packet: ResponderContextPacket,
        project_id: str,
        objective_id: str | None,
        comment_text: str,
    ) -> str:
        project = self.store.get_project(project_id)
        objective = self.store.get_objective(objective_id) if objective_id else None
        intent_model = self.store.latest_intent_model(objective_id) if objective_id else None
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control") if objective_id else None
        interrogation_review = self._interrogation_review(objective_id) if objective_id else {}
        task, run = self._latest_linked_task_and_run(project_id=project_id, objective_id=objective_id)
        run_output = self.run_cli_output(run.id) if run is not None else {}
        all_records = self.store.list_context_records(objective_id=objective_id) if objective_id else self.store.list_context_records(project_id=project_id)
        context_records = [
            {
                "record_type": record.record_type,
                "created_at": record.created_at.isoformat(),
                "author_type": record.author_type,
                "author_id": record.author_id,
                "visibility": record.visibility,
                "task_id": record.task_id,
                "run_id": record.run_id,
                "content": record.content,
                "metadata": record.metadata,
            }
            for record in all_records
        ]
        payload = {
            "project": serialize_dataclass(project) if project is not None else None,
            "mode": packet.mode,
            "next_action": {
                "title": packet.next_action_title,
                "body": packet.next_action_body,
            },
            "objective": serialize_dataclass(objective) if objective is not None else None,
            "intent_model": serialize_dataclass(intent_model) if intent_model is not None else None,
            "interrogation_review": interrogation_review,
            "mermaid": (
                {
                    "status": mermaid.status.value,
                    "summary": mermaid.summary,
                    "content": mermaid.content,
                    "version": mermaid.version,
                    "blocking_reason": mermaid.blocking_reason,
                }
                if mermaid is not None
                else None
            ),
            "latest_task": serialize_dataclass(task) if task is not None else None,
            "latest_run": serialize_dataclass(run) if run is not None else None,
            "latest_run_output": run_output,
            "recent_turns": [serialize_dataclass(turn) for turn in packet.recent_turns],
            "retrieved_memories": [serialize_dataclass(memory) for memory in packet.retrieved_memories],
            "frustration_detected": packet.frustration_detected,
            "all_context_records": context_records,
            "operator_message": comment_text,
        }
        return (
            "You are the accrivia-harness UI responder.\n"
            "Answer the operator's latest message directly and concretely.\n"
            "Use the full current objective context, not just the latest run.\n"
            "Do not dodge the question. Do not default to boilerplate about reviewing output unless that directly answers the question.\n"
            "If the operator asks where red-team belongs, answer that directly from the planning/control-flow context.\n"
            "Prefer plain language and explain what stage the operator is in when relevant.\n"
            "Return JSON only with keys: reply, recommended_action, evidence_refs, mode_shift.\n"
            "reply: short plain-language answer to the operator\n"
            "recommended_action: one of none, answer_prompt, review_mermaid, review_run, start_run, open_investigation\n"
            "evidence_refs: array of short strings\n"
            "mode_shift: one of none, investigation\n\n"
            f"Context:\n{json.dumps(payload, indent=2, sort_keys=True)}\n"
        )

    def _parse_ui_responder_response(self, text: str) -> dict[str, object] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        candidates.extend(fenced)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            reply = str(payload.get("reply") or "").strip()
            if not reply:
                continue
            recommended_action = str(payload.get("recommended_action") or "none").strip() or "none"
            mode_shift = str(payload.get("mode_shift") or "none").strip() or "none"
            evidence_refs = [
                str(item).strip()
                for item in list(payload.get("evidence_refs") or [])
                if str(item).strip()
            ]
            return {
                "reply": reply,
                "recommended_action": recommended_action,
                "mode_shift": mode_shift,
                "evidence_refs": evidence_refs,
            }
        return None

    def _generate_mermaid_update_proposal(self, objective_id: str, *, directive: str) -> dict[str, str] | None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return None
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is None or not getattr(llm_router, "executors", {}):
            return None
        intent_model = self.store.latest_intent_model(objective_id)
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        comments = self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")[-12:]
        run_dir = self.workspace_root / "ui_mermaid" / objective_id / new_id("proposal")
        run_dir.mkdir(parents=True, exist_ok=True)
        anchor_match = re.search(r"\[Mermaid anchor:\s*([^\]]+)\]", directive)
        anchor_label = anchor_match.group(1).strip() if anchor_match else ""
        rewrite_requested = bool(
            re.search(r"\b(rewrite|regenerate|redo|rebuild|start over|restructure|replace the diagram|full rewrite)\b", directive, flags=re.IGNORECASE)
        )
        edit_mode_instruction = (
            f"This is an anchored local edit request around the Mermaid element labeled '{anchor_label}'. "
            "Preserve the rest of the diagram unless the operator explicitly asks for broader restructuring. "
            "Make the smallest viable patch that satisfies the comment."
            if anchor_label and not rewrite_requested
            else "You may revise the full diagram as needed to satisfy the operator's requested process change."
        )
        prompt = (
            "You are updating a Mermaid flowchart for the accrivia-harness UI.\n"
            "Revise the workflow_control Mermaid to reflect the operator's requested process changes.\n"
            "Preserve valid parts of the current diagram, and avoid unnecessary rewrites.\n"
            f"{edit_mode_instruction}\n"
            "Return JSON only with keys: summary, content.\n"
            "summary: one short sentence explaining what changed\n"
            "content: full Mermaid flowchart text\n\n"
            f"Objective title: {objective.title}\n"
            f"Objective summary: {objective.summary}\n"
            f"Intent summary: {intent_model.intent_summary if intent_model else ''}\n"
            f"Success definition: {intent_model.success_definition if intent_model else ''}\n"
            f"Non-negotiables: {json.dumps(intent_model.non_negotiables if intent_model else [])}\n"
            f"Current Mermaid:\n{mermaid.content if mermaid else ''}\n\n"
            f"Operator directive: {directive}\n"
            f"Recent operator comments: {json.dumps([record.content for record in comments], indent=2)}\n"
        )
        task = Task(
            id=new_id("ui_mermaid_task"),
            project_id=objective.project_id,
            title=f"Propose Mermaid update for {objective.title}",
            objective="Generate a revised Mermaid diagram proposal from operator guidance.",
            strategy="ui_mermaid_proposal",
            status=TaskStatus.COMPLETED,
        )
        run = Run(
            id=new_id("ui_mermaid_run"),
            task_id=task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary=f"Mermaid proposal for {objective.id}",
        )
        try:
            result, backend = llm_router.execute(LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir))
        except LLMExecutionError:
            return None
        parsed = self._parse_mermaid_update_response(result.response_text)
        if parsed is None:
            return None
        parsed["backend"] = backend
        parsed["prompt_path"] = str(result.prompt_path)
        parsed["response_path"] = str(result.response_path)
        return parsed

    def _parse_mermaid_update_response(self, text: str) -> dict[str, str] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        candidates.extend(fenced)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            summary = str(payload.get("summary") or "").strip()
            content = str(payload.get("content") or "").strip()
            if summary and content:
                return {"summary": summary, "content": content}
        return None

    def _proposal_record(self, objective_id: str, proposal_id: str) -> ContextRecord | None:
        for record in self.store.list_context_records(objective_id=objective_id, record_type="mermaid_update_proposed"):
            if record.id == proposal_id:
                return record
        return None

    def _latest_mermaid_proposal(self, objective_id: str) -> dict[str, object] | None:
        proposals = self.store.list_context_records(objective_id=objective_id, record_type="mermaid_update_proposed")
        if not proposals:
            return None
        resolutions = {
            str(record.metadata.get("proposal_id") or "")
            for record in self.store.list_context_records(objective_id=objective_id)
            if record.record_type in {"mermaid_update_accepted", "mermaid_update_rejected", "mermaid_update_rewound"}
        }
        proposal = proposals[-1]
        if proposal.id in resolutions:
            return None
        return {
            "id": proposal.id,
            "summary": proposal.content,
            "content": str(proposal.metadata.get("content") or ""),
            "directive": str(proposal.metadata.get("directive") or ""),
            "backend": str(proposal.metadata.get("backend") or ""),
            "created_at": proposal.created_at.isoformat(),
        }

    def _atomic_generation_state(self, objective_id: str) -> dict[str, object]:
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        diagram_version = mermaid.version if mermaid is not None else None
        starts = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_started")
            if diagram_version is None or int(record.metadata.get("diagram_version") or 0) == diagram_version
        ]
        if not starts:
            return {
                "status": "idle",
                "diagram_version": diagram_version,
                "generation_id": "",
                "started_at": "",
                "completed_at": "",
                "failed_at": "",
                "unit_count": 0,
            }
        start = starts[-1]
        generation_id = str(start.metadata.get("generation_id") or start.id)
        completed = next(
            (
                record
                for record in reversed(self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_completed"))
                if str(record.metadata.get("generation_id") or "") == generation_id
            ),
            None,
        )
        failed = next(
            (
                record
                for record in reversed(self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_failed"))
                if str(record.metadata.get("generation_id") or "") == generation_id
            ),
            None,
        )
        unit_count = len(
            [
                record
                for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_unit_generated")
                if str(record.metadata.get("generation_id") or "") == generation_id
            ]
        )
        progress = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_progress")
            if str(record.metadata.get("generation_id") or "") == generation_id
        ]
        status = "running"
        if failed is not None:
            status = "failed"
        elif completed is not None:
            status = "completed"
        phase = ""
        if status == "completed":
            phase = "complete"
        elif status == "failed":
            phase = "failed"
        elif progress:
            phase = str(progress[-1].metadata.get("phase") or "")
        related_times = [start.created_at]
        if progress:
            related_times.extend(record.created_at for record in progress)
        related_times.extend(
            record.created_at
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_unit_generated")
            if str(record.metadata.get("generation_id") or "") == generation_id
        )
        if completed is not None:
            related_times.append(completed.created_at)
        if failed is not None:
            related_times.append(failed.created_at)
        last_activity_at = max(related_times).isoformat() if related_times else ""
        # Extract refinement round and latest critique/coverage from telemetry
        telemetry = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_decomposition_telemetry")
            if str(record.metadata.get("generation_id") or "") == generation_id
        ]
        refinement_round = 0
        critique_accepted = None
        coverage_complete = None
        last_critique_problems = []
        last_coverage_gaps = []
        for record in telemetry:
            evt = record.metadata.get("event_type", "")
            rnd = record.metadata.get("round")
            if rnd is not None and int(rnd) > refinement_round:
                refinement_round = int(rnd)
            if evt == "critique":
                critique_accepted = record.metadata.get("accepted")
                last_critique_problems = list(record.metadata.get("problems") or [])
            if evt == "coverage":
                coverage_complete = record.metadata.get("complete")
                last_coverage_gaps = list(record.metadata.get("gaps") or [])
        return {
            "status": status,
            "diagram_version": diagram_version,
            "generation_id": generation_id,
            "started_at": start.created_at.isoformat(),
            "completed_at": completed.created_at.isoformat() if completed is not None else "",
            "failed_at": failed.created_at.isoformat() if failed is not None else "",
            "unit_count": unit_count,
            "phase": phase,
            "last_activity_at": last_activity_at,
            "error": failed.content if failed is not None else "",
            "refinement_round": refinement_round,
            "critique_accepted": critique_accepted,
            "coverage_complete": coverage_complete,
            "last_critique_problems": last_critique_problems,
            "last_coverage_gaps": last_coverage_gaps,
        }

    def _atomic_units_for_objective(
        self,
        objective_id: str,
        linked_tasks: list[Task],
        generation_state: dict[str, object],
    ) -> list[dict[str, object]]:
        generation_id = str(generation_state.get("generation_id") or "")
        if not generation_id:
            return []
        tasks_by_id = {task.id: task for task in linked_tasks}
        task_runs = {task.id: self.store.list_runs(task.id) for task in linked_tasks}
        units: list[dict[str, object]] = []
        records = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_unit_generated")
            if str(record.metadata.get("generation_id") or "") == generation_id
        ]
        published_task_ids: set[str] = set()

        for record in records:
            task_id = str(record.metadata.get("task_id") or "")
            if task_id:
                published_task_ids.add(task_id)
            task = tasks_by_id.get(task_id)
            runs = task_runs.get(task_id, [])
            latest_run = runs[-1] if runs else None

            status = task.status.value if task is not None else "pending"

            # Read validation results from the report artifact if available.
            validation_info = None
            if latest_run is not None:
                report_artifacts = [
                    a for a in self.store.list_artifacts(latest_run.id)
                    if a.kind == "report" and a.path
                ]
                if report_artifacts:
                    try:
                        import json as _json
                        report_data = _json.loads(Path(report_artifacts[-1].path).read_text(encoding="utf-8"))
                        cc = report_data.get("compile_check")
                        tc = report_data.get("test_check")
                        if cc is not None or tc is not None:
                            validation_info = {
                                "compile_passed": bool(cc.get("passed")) if cc else None,
                                "test_passed": bool(tc.get("passed")) if tc else None,
                                "test_timed_out": bool(tc.get("timed_out")) if tc else False,
                            }
                    except Exception:
                        pass

            units.append(
                {
                    "id": task_id or record.id,
                    "title": str(record.metadata.get("title") or (task.title if task else record.content)),
                    "objective": str(record.metadata.get("objective") or (task.objective if task else "")),
                    "rationale": str(record.metadata.get("rationale") or ""),
                    "strategy": str(record.metadata.get("strategy") or (task.strategy if task else "")),
                    "status": status,
                    "order": int(record.metadata.get("order") or 0),
                    "published_unit": True,
                    "latest_run": (
                        {
                            "attempt": latest_run.attempt,
                            "status": latest_run.status.value,
                            "started_at": latest_run.created_at.isoformat() if latest_run.created_at else None,
                            "finished_at": latest_run.updated_at.isoformat() if latest_run.status.value in ("completed", "failed", "blocked", "disposed") and latest_run.updated_at else None,
                            "validation": validation_info,
                        }
                        if latest_run is not None
                        else None
                    ),
                }
            )
        next_order = len(units) + 1
        for task in linked_tasks:
            if task.id in published_task_ids:
                continue
            runs = task_runs.get(task.id, [])
            latest_run = runs[-1] if runs else None
            validation_info = None
            if latest_run is not None:
                report_artifacts = [
                    a for a in self.store.list_artifacts(latest_run.id)
                    if a.kind == "report" and a.path
                ]
                if report_artifacts:
                    try:
                        import json as _json
                        report_data = _json.loads(Path(report_artifacts[-1].path).read_text(encoding="utf-8"))
                        cc = report_data.get("compile_check")
                        tc = report_data.get("test_check")
                        if cc is not None or tc is not None:
                            validation_info = {
                                "compile_passed": bool(cc.get("passed")) if cc else None,
                                "test_passed": bool(tc.get("passed")) if tc else None,
                                "test_timed_out": bool(tc.get("timed_out")) if tc else False,
                            }
                    except Exception:
                        pass
            units.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "objective": task.objective,
                    "rationale": "",
                    "strategy": task.strategy,
                    "status": task.status.value,
                    "order": next_order,
                    "published_unit": False,
                    "latest_run": (
                        {
                            "attempt": latest_run.attempt,
                            "status": latest_run.status.value,
                            "started_at": latest_run.created_at.isoformat() if latest_run.created_at else None,
                            "finished_at": latest_run.updated_at.isoformat() if latest_run.status.value in ("completed", "failed", "blocked", "disposed") and latest_run.updated_at else None,
                            "validation": validation_info,
                        }
                        if latest_run is not None
                        else None
                    ),
                }
            )
            next_order += 1
        return sorted(units, key=lambda item: (int(item["order"]), str(item["title"])))

    def _promotion_review_for_objective(
        self,
        objective_id: str,
        linked_tasks: list[Task],
    ) -> dict[str, object]:
        objective_review_state = self._objective_review_state(objective_id)
        promotions_by_task = {
            task.id: [serialize_dataclass(promotion) for promotion in self.store.list_promotions(task.id)]
            for task in linked_tasks
        }
        tasks_by_id = {task.id: task for task in linked_tasks}
        objective_records = self.store.list_context_records(objective_id=objective_id)
        review_start_records = [record for record in objective_records if record.record_type == "objective_review_started"]
        review_completed_records = [record for record in objective_records if record.record_type == "objective_review_completed"]
        review_failed_records = [record for record in objective_records if record.record_type == "objective_review_failed"]
        review_packet_records = [record for record in objective_records if record.record_type == "objective_review_packet"]
        review_cycle_artifact_records = [record for record in objective_records if record.record_type == "objective_review_cycle_artifact"]
        worker_response_records = [record for record in objective_records if record.record_type == "objective_review_worker_response"]
        reviewer_rebuttal_records = [record for record in objective_records if record.record_type == "objective_review_reviewer_rebuttal"]
        override_records = [record for record in objective_records if record.record_type == "objective_review_override_approved"]
        waivers_by_task: dict[str, dict[str, object]] = {}
        for record in objective_records:
            if record.record_type != "failed_task_waived":
                continue
            task_id = str(record.metadata.get("task_id") or "")
            if not task_id:
                continue
            waivers_by_task[task_id] = {
                "record_id": record.id,
                "rationale": record.content,
                "created_at": record.created_at.isoformat(),
                "disposition": record.metadata.get("disposition"),
            }
        counts = {"completed": 0, "active": 0, "pending": 0, "failed": 0}
        for task in linked_tasks:
            status = task.status.value
            if status in counts:
                counts[status] += 1
        failed_entries: list[dict[str, object]] = []
        unresolved_failed_count = 0
        waived_failed_count = 0
        for task in linked_tasks:
            if task.status.value != "failed":
                continue
            waiver = waivers_by_task.get(task.id)
            metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
            disposition = metadata.get("failed_task_disposition") if isinstance(metadata.get("failed_task_disposition"), dict) else None
            effective_status = "waived" if waiver or (disposition and str(disposition.get("kind") or "") == "waive_obsolete") else "blocking"
            if effective_status == "waived":
                waived_failed_count += 1
            else:
                unresolved_failed_count += 1
            failed_entries.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "objective": task.objective,
                    "status": task.status.value,
                    "effective_status": effective_status,
                    "disposition": disposition,
                    "waiver": waiver,
                }
            )
        review_packets: list[dict[str, object]] = []
        for task in linked_tasks:
            promotions = promotions_by_task.get(task.id) or []
            if not promotions:
                continue
            latest = promotions[-1]
            validators = latest.get("details", {}).get("validators", []) if isinstance(latest.get("details"), dict) else []
            issues = [
                issue
                for validator in validators if isinstance(validator, dict)
                for issue in validator.get("issues", [])
                if isinstance(issue, dict)
            ]
            review_packets.append(
                {
                    "source": "task_promotion",
                    "task_id": task.id,
                    "task_title": task.title,
                    "task_status": task.status.value,
                    "latest": latest,
                    "all": promotions,
                    "issue_count": len(issues),
                }
            )
        ready = counts["active"] == 0 and counts["pending"] == 0 and unresolved_failed_count == 0
        remediation_tasks_by_review: dict[str, list[Task]] = {}
        for task in linked_tasks:
            metadata = task.external_ref_metadata if isinstance(task.external_ref_metadata, dict) else {}
            remediation = metadata.get("objective_review_remediation") if isinstance(metadata.get("objective_review_remediation"), dict) else None
            review_id = str(remediation.get("review_id") or "") if remediation else ""
            if not review_id:
                continue
            remediation_tasks_by_review.setdefault(review_id, []).append(task)
        round_rows: list[dict[str, object]] = []
        start_order = sorted(review_start_records, key=lambda record: record.created_at)
        for idx, start in enumerate(start_order, start=1):
            review_id = str(start.metadata.get("review_id") or start.id)
            packets = []
            for record in review_packet_records:
                if str(record.metadata.get("review_id") or "") != review_id:
                    continue
                llm_usage, llm_usage_reported, llm_usage_source = self._normalize_objective_review_usage_metadata(record.metadata)
                packets.append(
                    {
                        "source": "objective_review",
                        "review_id": review_id,
                        "reviewer": str(record.metadata.get("reviewer") or ""),
                        "dimension": str(record.metadata.get("dimension") or ""),
                        "verdict": str(record.metadata.get("verdict") or ""),
                        "progress_status": str(record.metadata.get("progress_status") or "not_applicable"),
                        "severity": str(record.metadata.get("severity") or ""),
                        "owner_scope": str(record.metadata.get("owner_scope") or ""),
                        "summary": record.content,
                        "findings": list(record.metadata.get("findings") or []),
                        "evidence": list(record.metadata.get("evidence") or []),
                        "required_artifact_type": str(record.metadata.get("required_artifact_type") or ""),
                        "artifact_schema": record.metadata.get("artifact_schema") if isinstance(record.metadata.get("artifact_schema"), dict) else {},
                        "evidence_contract": self._objective_review_evidence_contract(record.metadata),
                        "closure_criteria": str(record.metadata.get("closure_criteria") or ""),
                        "evidence_required": str(record.metadata.get("evidence_required") or ""),
                        "repeat_reason": str(record.metadata.get("repeat_reason") or ""),
                        "llm_usage": llm_usage,
                        "llm_usage_reported": llm_usage_reported,
                        "llm_usage_source": llm_usage_source,
                        "backend": record.metadata.get("backend"),
                        "created_at": record.created_at.isoformat(),
                    }
                )
            completed = next(
                (record for record in reversed(review_completed_records) if str(record.metadata.get("review_id") or "") == review_id),
                None,
            )
            failed = next(
                (record for record in reversed(review_failed_records) if str(record.metadata.get("review_id") or "") == review_id),
                None,
            )
            verdict_counts = {"pass": 0, "concern": 0, "remediation_required": 0}
            for packet in packets:
                verdict = str(packet.get("verdict") or "")
                if verdict in verdict_counts:
                    verdict_counts[verdict] += 1
            remediation_tasks = remediation_tasks_by_review.get(review_id, [])
            review_cycle_artifact = next(
                (
                    record for record in reversed(review_cycle_artifact_records)
                    if str(record.metadata.get("review_id") or "") == review_id
                ),
                None,
            )
            worker_responses = [
                {
                    "record_id": record.id,
                    "task_id": str(record.metadata.get("task_id") or ""),
                    "run_id": str(record.metadata.get("run_id") or ""),
                    "dimension": str(record.metadata.get("dimension") or ""),
                    "finding_record_id": str(record.metadata.get("finding_record_id") or ""),
                    "exact_artifact_produced": record.metadata.get("exact_artifact_produced"),
                    "closure_mapping": str(record.metadata.get("closure_mapping") or ""),
                    "created_at": record.created_at.isoformat(),
                }
                for record in worker_response_records
                if str(record.metadata.get("review_id") or "") == review_id
            ]
            reviewer_rebuttals = [
                {
                    "record_id": record.id,
                    "prior_review_id": str(record.metadata.get("prior_review_id") or ""),
                    "dimension": str(record.metadata.get("dimension") or ""),
                    "outcome": str(record.metadata.get("outcome") or ""),
                    "reason": str(record.metadata.get("reason") or ""),
                    "created_at": record.created_at.isoformat(),
                }
                for record in reviewer_rebuttal_records
                if str(record.metadata.get("review_id") or "") == review_id
            ]
            operator_override = next(
                (
                    record for record in reversed(override_records)
                    if str(record.metadata.get("review_id") or "") == review_id
                ),
                None,
            )
            remediation_counts = {"total": len(remediation_tasks), "completed": 0, "active": 0, "pending": 0, "failed": 0}
            for task in remediation_tasks:
                if task.status.value in remediation_counts:
                    remediation_counts[task.status.value] += 1
            needs_remediation = verdict_counts["concern"] > 0 or verdict_counts["remediation_required"] > 0
            status = "running"
            if failed is not None:
                status = "failed"
            elif completed is not None:
                if needs_remediation:
                    if remediation_counts["active"] > 0 or remediation_counts["pending"] > 0:
                        status = "remediating"
                    elif remediation_counts["total"] > 0 and remediation_counts["failed"] == 0 and remediation_counts["completed"] == remediation_counts["total"]:
                        status = "ready_for_rerun"
                    else:
                        status = "needs_remediation"
                else:
                    status = "passed"
            if operator_override is not None:
                status = "passed"
            round_activity = [start.created_at]
            round_activity.extend(record.created_at for record in review_packet_records if str(record.metadata.get("review_id") or "") == review_id)
            if completed is not None:
                round_activity.append(completed.created_at)
            if failed is not None:
                round_activity.append(failed.created_at)
            round_rows.append(
                {
                    "review_id": review_id,
                    "round_number": idx,
                    "status": status,
                    "started_at": start.created_at.isoformat(),
                    "completed_at": completed.created_at.isoformat() if completed is not None else "",
                    "failed_at": failed.created_at.isoformat() if failed is not None else "",
                    "last_activity_at": max(round_activity).isoformat() if round_activity else "",
                    "packet_count": len(packets),
                    "verdict_counts": verdict_counts,
                    "packets": sorted(
                        packets,
                        key=lambda item: (str(item.get("created_at") or ""), str(item.get("dimension") or "")),
                        reverse=True,
                    ),
                    "review_cycle_artifact": {
                        "record_id": review_cycle_artifact.id,
                        "start_event": review_cycle_artifact.metadata.get("start_event"),
                        "packet_persistence_events": list(review_cycle_artifact.metadata.get("packet_persistence_events") or []),
                        "terminal_event": review_cycle_artifact.metadata.get("terminal_event"),
                        "linked_outcome": review_cycle_artifact.metadata.get("linked_outcome"),
                    } if review_cycle_artifact is not None else {},
                    "operator_override": {
                        "record_id": operator_override.id,
                        "rationale": str(operator_override.metadata.get("rationale") or operator_override.content or ""),
                        "author": str(operator_override.metadata.get("author") or operator_override.author_type or "operator"),
                        "created_at": operator_override.created_at.isoformat(),
                        "waived_task_ids": list(operator_override.metadata.get("waived_task_ids") or []),
                    } if operator_override is not None else {},
                    "worker_responses": sorted(worker_responses, key=lambda item: str(item.get("created_at") or ""), reverse=True),
                    "reviewer_rebuttals": sorted(reviewer_rebuttals, key=lambda item: str(item.get("created_at") or ""), reverse=True),
                    "remediation_counts": remediation_counts,
                    "remediation_tasks": [
                        {"id": task.id, "title": task.title, "status": task.status.value}
                        for task in sorted(remediation_tasks, key=lambda item: item.created_at)
                    ],
                    "needs_remediation": needs_remediation,
                }
            )
        review_rounds = sorted(round_rows, key=lambda item: int(item.get("round_number") or 0), reverse=True)
        latest_round = review_rounds[0] if review_rounds else None
        latest_override = (
            latest_round.get("operator_override")
            if isinstance(latest_round, dict) and isinstance(latest_round.get("operator_override"), dict)
            else {}
        )
        objective_review_packets = list(latest_round.get("packets") or []) if isinstance(latest_round, dict) else []
        all_review_packets = objective_review_packets + review_packets
        all_review_packets.sort(
            key=lambda item: (
                str(
                    item.get("created_at")
                    or (item.get("latest") or {}).get("created_at")
                    or ""
                ),
                str(item.get("task_title") or item.get("reviewer") or ""),
            ),
            reverse=True,
        )
        verdict_counts = {"pass": 0, "concern": 0, "remediation_required": 0}
        for packet in objective_review_packets:
            verdict = str(packet.get("verdict") or "").strip()
            if verdict in verdict_counts:
                verdict_counts[verdict] += 1
        latest_round_status = str(latest_round.get("status") or "") if isinstance(latest_round, dict) else ""
        can_start_new_round = bool(ready) and (
            latest_round is None
            or latest_round_status in {"ready_for_rerun", "failed"}
            or (
                latest_round_status == "passed"
                and bool(latest_round.get("completed_at"))
                and objective_review_state.get("review_id") != str(latest_round.get("review_id") or "")
            )
        )
        override_active = bool(latest_override)
        review_clear = ready and bool(latest_round) and (
            override_active or (verdict_counts["concern"] == 0 and verdict_counts["remediation_required"] == 0)
        )
        phase = "promotion_review_pending" if ready and not latest_round else "promotion_review_active" if latest_round else "execution"
        if counts["active"] > 0 or counts["pending"] > 0:
            next_action = "Review findings were turned into remediation tasks. Continue in Atomic while the harness works through them."
            phase = "execution"
        elif unresolved_failed_count:
            next_action = "Resolve or disposition the remaining failed tasks before promotion can proceed."
            phase = "remediation_required"
        elif override_active:
            next_action = "The latest promotion review round was operator-approved. The objective is clear to promote."
            phase = "promotion_review_active"
        elif verdict_counts["remediation_required"] > 0 or verdict_counts["concern"] > 0:
            concern_total = verdict_counts["remediation_required"] + verdict_counts["concern"]
            if latest_round_status == "ready_for_rerun":
                next_action = f"Remediation from promotion review round {latest_round.get('round_number')} is complete. The harness should start the next review round now."
                phase = "promotion_review_pending"
            else:
                next_action = f"Promotion review found {concern_total} issue(s). Route remediation back into Atomic before promoting."
                phase = "remediation_required"
        elif latest_round_status == "running":
            next_action = f"Promotion review round {latest_round.get('round_number')} is running. Reviewer packets will appear as each agent finishes."
            phase = "promotion_review_active"
        elif latest_round:
            next_action = "Review the latest promotion packets and LLM affirmation details, then decide whether to promote the objective."
        else:
            next_action = "Execution is complete and no blockers remain. Automatic promotion review should begin next."
        recommended_view = "promotion-review" if ready and phase != "execution" else "atomic"
        return {
            "ready": ready,
            "review_clear": review_clear,
            "phase": phase,
            "recommended_view": recommended_view,
            "objective_review_state": objective_review_state,
            "verdict_counts": verdict_counts,
            "task_counts": counts,
            "waived_failed_count": waived_failed_count,
            "unresolved_failed_count": unresolved_failed_count,
            "review_packet_count": len(all_review_packets),
            "objective_review_packet_count": sum(int((round_row.get("packet_count") or 0)) for round_row in review_rounds),
            "review_rounds": review_rounds,
            "can_start_new_round": can_start_new_round,
            "can_force_promote": bool(latest_round) and not override_active and counts["active"] == 0 and counts["pending"] == 0,
            "operator_override": latest_override,
            "review_packets": all_review_packets,
            "failed_tasks": failed_entries,
            "next_action": next_action,
        }

    def _build_responder_context_packet(
        self,
        *,
        project_id: str,
        objective_id: str | None,
        comment_text: str,
        frustration_detected: bool,
    ) -> ResponderContextPacket:
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        objective = self.store.get_objective(objective_id) if objective_id else None
        intent_model = self.store.latest_intent_model(objective_id) if objective_id else None
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control") if objective_id else None
        next_action = self._next_action_for_context(objective_id)
        task, run = self._latest_linked_task_and_run(project_id=project_id, objective_id=objective_id)
        run_context = None
        if run is not None:
            run_context = RunResponderContext(
                run_id=run.id,
                attempt=run.attempt,
                status=run.status.value,
                summary=(run.summary or "").strip(),
                available_sections=[section.label for section in self._run_output_sections(run.id)],
                section_previews={
                    section.label: self._truncate_text(section.content, 220)
                    for section in self._run_output_sections(run.id)
                },
            )
        task_context = None
        if task is not None:
            task_context = TaskResponderContext(
                task_id=task.id,
                title=task.title,
                status=task.status.value,
                strategy=task.strategy,
                objective=task.objective,
            )
        objective_context = None
        if objective is not None:
            objective_context = ObjectiveResponderContext(
                objective_id=objective.id,
                title=objective.title,
                status=objective.status.value,
                summary=objective.summary,
                intent_summary=(intent_model.intent_summary if intent_model is not None else ""),
                success_definition=(intent_model.success_definition if intent_model is not None else ""),
                non_negotiables=(intent_model.non_negotiables if intent_model is not None else []),
                mermaid_status=(mermaid.status.value if mermaid is not None else ""),
                mermaid_summary=(mermaid.summary if mermaid is not None else ""),
            )
        retrieved_memories = self.memory_provider.retrieve(
            project_id=project.id,
            objective_id=objective_id,
            query_text=comment_text,
            limit=4,
        )
        current_mode = "empty"
        interrogation_question = ""
        interrogation_remaining = 0
        if objective is not None:
            current_mode = self._focus_mode_for_objective(objective.id)
            if current_mode == "interrogation_review":
                review = self._interrogation_review(objective.id)
                questions = list(review.get("questions") or [])
                intent_created_at = intent_model.created_at.isoformat() if intent_model is not None else ""
                relevant_answers = [
                    record
                    for record in self.store.list_context_records(objective_id=objective.id, record_type="operator_comment")
                    if not intent_created_at or record.created_at.isoformat() >= intent_created_at
                ]
                question_index = min(len(relevant_answers), max(0, len(questions) - 1))
                if questions:
                    interrogation_question = questions[question_index]
                    interrogation_remaining = max(0, len(questions) - question_index - 1)
        return ResponderContextPacket(
            project_id=project.id,
            project_name=project.name,
            mode=current_mode,
            next_action_title=next_action["title"],
            next_action_body=next_action["body"],
            objective=objective_context,
            task=task_context,
            run=run_context,
            recent_turns=self._recent_conversation_turns(project_id=project_id, objective_id=objective_id),
            frustration_detected=frustration_detected,
            retrieved_memories=retrieved_memories,
            interrogation_question=interrogation_question,
            interrogation_remaining=interrogation_remaining,
        )

    def _log_ui_memory_retrieval(
        self,
        *,
        project_id: str,
        objective_id: str | None,
        comment_text: str,
        responder_result: ResponderResult,
    ) -> None:
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="ui_memory_retrieval",
                project_id=project_id,
                objective_id=objective_id,
                visibility="system_only",
                author_type="system",
                content=comment_text,
                metadata={
                    "retrieved_count": len(responder_result.retrieved_memories),
                    "retrieved_memories": [serialize_dataclass(memory) for memory in responder_result.retrieved_memories],
                    "recommended_action": responder_result.recommended_action,
                    "mode_shift": responder_result.mode_shift,
                    "evidence_refs": responder_result.evidence_refs,
                },
            )
        )

    def _recent_conversation_turns(self, *, project_id: str, objective_id: str | None) -> list[ConversationTurn]:
        turns: list[ConversationTurn] = []
        for record_type, role in (("operator_comment", "operator"), ("harness_reply", "harness")):
            for record in self.store.list_context_records(
                project_id=project_id,
                objective_id=objective_id,
                record_type=record_type,
            ):
                turns.append(
                    ConversationTurn(
                        role=role,
                        text=record.content,
                        created_at=record.created_at.isoformat(),
                    )
                )
        turns.sort(key=lambda item: item.created_at)
        return turns[-10:]

    def _latest_linked_task_and_run(self, *, project_id: str, objective_id: str | None):
        linked_tasks = [
            task
            for task in self.store.list_tasks(project_id)
            if objective_id and task.objective_id == objective_id
        ]
        if not linked_tasks:
            return None, None
        best_pair = None
        for candidate in linked_tasks:
            candidate_runs = self.store.list_runs(candidate.id)
            candidate_latest_run = candidate_runs[-1] if candidate_runs else None
            candidate_sort_key = (
                candidate_latest_run.created_at if candidate_latest_run is not None else candidate.updated_at,
                candidate.id,
            )
            if best_pair is None or candidate_sort_key > best_pair[0]:
                best_pair = (candidate_sort_key, candidate, candidate_latest_run)
        assert best_pair is not None
        return best_pair[1], best_pair[2]

    def _truncate_text(self, text: str, limit: int) -> str:
        cleaned = " ".join((text or "").split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)].rstrip() + "..."

    def _next_action_for_context(self, objective_id: str | None) -> dict[str, str]:
        if objective_id is None:
            return {
                "title": "Create or select an objective",
                "body": "Choose one objective to continue.",
            }
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return {
                "title": "Objective missing",
                "body": "The selected objective no longer exists.",
            }
        if not self.store.latest_intent_model(objective.id):
            return {
                "title": "Answer the desired outcome",
                "body": "Describe the result you want from this objective.",
            }
        review = self._interrogation_review(objective.id)
        if not review.get("completed"):
            return {
                "title": "Answer the next red-team question",
                "body": "The harness is interrogating and red-teaming the plan in the transcript before Mermaid review.",
            }
        latest_mermaid = self.store.latest_mermaid_artifact(objective.id, "workflow_control")
        if latest_mermaid is None or latest_mermaid.status != MermaidStatus.FINISHED:
            return {
                "title": "Finish or pause Mermaid review",
                "body": "Execution stays blocked until the current Mermaid is finished.",
            }
        gate = objective_execution_gate(self.store, objective.id)
        if not gate.ready:
            blocked = [check for check in gate.gate_checks if not check["ok"]]
            if blocked:
                return {
                    "title": str(blocked[0]["label"]),
                    "body": str(blocked[0].get("detail") or "That gate is still blocking execution."),
                }
        task, run = self._latest_linked_task_and_run(project_id=objective.project_id, objective_id=objective.id)
        if task is None:
            return {
                "title": "Create the first bounded slice",
                "body": "The harness should create the first bounded implementation step from the approved intent and Mermaid.",
            }
        if run is None:
            return {
                "title": "Ready to run the first implementation step",
                "body": "Start the current implementation step when you are ready.",
            }
        return {
            "title": "Review the latest attempt",
            "body": "Review the latest run evidence before deciding whether to continue, revise, or investigate.",
        }

    def _focus_mode_for_objective(self, objective_id: str) -> str:
        intent_model = self.store.latest_intent_model(objective_id)
        if intent_model is None or not (intent_model.intent_summary or "").strip():
            return "desired_outcome"
        if not (intent_model.success_definition or "").strip():
            return "success_definition"
        if not list(intent_model.non_negotiables):
            return "non_negotiables"
        review = self._interrogation_review(objective_id)
        if not review.get("completed"):
            return "interrogation_review"
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        if mermaid is None or mermaid.status != MermaidStatus.FINISHED:
            return "mermaid_review"
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return "empty"
        task, run = self._latest_linked_task_and_run(project_id=objective.project_id, objective_id=objective.id)
        if task is None or run is None:
            return "run_start"
        return "run_review"

    def _create_seed_mermaid(self, objective: Objective) -> MermaidArtifact:
        artifact = MermaidArtifact(
            id=new_id("diagram"),
            objective_id=objective.id,
            diagram_type="workflow_control",
            version=self.store.next_mermaid_version(objective.id, "workflow_control"),
            status=MermaidStatus.DRAFT,
            summary="Initial workflow draft",
            content=self._default_objective_mermaid(objective),
            required_for_execution=True,
            blocking_reason="Workflow review has not been completed yet.",
            author_type="system",
        )
        self.store.create_mermaid_artifact(artifact)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="mermaid_seeded",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="model_visible",
                author_type="system",
                content="Seeded initial required Mermaid workflow.",
                metadata={
                    "diagram_id": artifact.id,
                    "diagram_type": artifact.diagram_type,
                    "version": artifact.version,
                    "status": artifact.status.value,
                },
            )
        )
        return artifact

    def _project_mermaid(self, project_id: str, tasks, runs_by_task: dict[str, list[Any]]) -> str:
        project = self.store.get_project(project_id)
        title = project.name if project is not None else project_id
        lines = ["flowchart TD", f'    P["Project: {self._mermaid_label(title)}"]']
        sorted_tasks = sorted(tasks, key=lambda item: (item.created_at, item.priority, item.id))
        latest_run_ids: list[str] = []
        for index, task in enumerate(sorted_tasks, start=1):
            task_node = f"T{index}"
            task_label = f"Task: {task.title}\\n{task.status.value} · {task.strategy}"
            lines.append(f'    {task_node}["{self._mermaid_label(task_label)}"]')
            if task.parent_task_id:
                parent_index = next(
                    (i for i, candidate in enumerate(sorted_tasks, start=1) if candidate.id == task.parent_task_id),
                    None,
                )
                if parent_index is not None:
                    lines.append(f"    T{parent_index} --> {task_node}")
                else:
                    lines.append(f"    P --> {task_node}")
            else:
                lines.append(f"    P --> {task_node}")
            runs = runs_by_task.get(task.id, [])
            if runs:
                latest_run = runs[-1]
                latest_run_ids.append(latest_run.id)
                run_node = f"R{index}"
                run_label = f"Run {latest_run.attempt}\\n{latest_run.status.value}"
                lines.append(f'    {run_node}["{self._mermaid_label(run_label)}"]')
                lines.append(f"    {task_node} --> {run_node}")
        if not sorted_tasks:
            lines.append('    P --> I["No tasks yet"]')
        return "\n".join(lines)

    def _default_objective_mermaid(self, objective: Objective) -> str:
        return "\n".join(
            [
                "flowchart TD",
                f'    A["Objective: {self._mermaid_label(objective.title)}"]',
                '    B["Intent Model"]',
                '    C["Mermaid Review"]',
                '    D["Plan"]',
                '    E["Atomic Slice"]',
                '    F["Execution"]',
                "    A --> B",
                "    B --> C",
                "    C --> D",
                "    D --> E",
                "    E --> F",
            ]
        )

    @staticmethod
    def _mermaid_label(value: str) -> str:
        return value.replace('"', "'")

    @staticmethod
    def _comment_looks_like_frustration(text: str) -> bool:
        lowered = text.lower()
        triggers = [
            "frustrat",
            "annoy",
            "confus",
            "stuck",
            "what am i supposed",
            "doesn't make sense",
            "terrible",
            "bad ux",
        ]
        return any(trigger in lowered for trigger in triggers)

    def _comment_requests_mermaid_update(
        self,
        text: str,
        *,
        project_id: str,
        objective_id: str | None,
    ) -> bool:
        lowered = text.lower().strip()
        mermaid_terms = ("mermaid", "diagram", "control flow", "flowchart", "flow chart")
        update_terms = ("update", "revise", "regenerate", "rewrite", "reflect this", "change", "remove", "add", "fix")
        if any(term in lowered for term in mermaid_terms) and any(term in lowered for term in update_terms):
            return True
        latest_mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control") if objective_id else None
        proposal_pending = self._latest_mermaid_proposal(objective_id) is not None if objective_id else False
        in_mermaid_review = latest_mermaid is not None and latest_mermaid.status in {MermaidStatus.PAUSED, MermaidStatus.DRAFT}
        structural_terms = ("step", "loop", "gate", "branch", "path", "node", "box", "label", "exit condition", "planning elements")
        if in_mermaid_review and any(term in lowered for term in update_terms) and (
            proposal_pending or any(term in lowered for term in structural_terms)
        ):
            return True
        if lowered in {"do it", "do it.", "do that", "apply it", "make the changes", "make your changes", "go ahead", "use that"}:
            recent_turns = self._recent_conversation_turns(project_id=project_id, objective_id=objective_id)
            recent_text = "\n".join(turn.text.lower() for turn in recent_turns[-6:])
            return (
                "update the mermaid" in recent_text
                or "proposed mermaid update" in recent_text
                or "diagram should be revised" in recent_text
                or "revise that diagram" in recent_text
                or "make your changes to the diagram" in recent_text
            )
        return False

    def _run_output_sections(self, run_id: str) -> list[RunOutputSection]:
        run_dir = self.workspace_root / "runs" / run_id
        candidates: list[tuple[str, Path]] = []
        for artifact in self.store.list_artifacts(run_id):
            candidates.append((artifact.kind, Path(artifact.path)))
        for label, filename in [
            ("plan", "plan.txt"),
            ("report", "report.json"),
            ("compile_output", "compile_output.txt"),
            ("test_output", "test_output.txt"),
            ("worker_stdout", "worker.stdout.txt"),
            ("worker_stderr", "worker.stderr.txt"),
            ("llm_stdout", "llm.stdout.txt"),
            ("llm_stderr", "llm.stderr.txt"),
            ("codex_worker_stdout", "codex_worker.stdout.txt"),
            ("codex_worker_stderr", "codex_worker.stderr.txt"),
            ("atomicity_telemetry", "atomicity_telemetry.json"),
        ]:
            path = run_dir / filename
            if path.exists():
                candidates.append((label, path))
        seen: set[str] = set()
        sections: list[RunOutputSection] = []
        for label, path in candidates:
            resolved = str(path.resolve())
            if resolved in seen or not path.exists() or not path.is_file():
                continue
            seen.add(resolved)
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if not content:
                continue
            sections.append(
                RunOutputSection(
                    label=label.replace("_", " "),
                    path=resolved,
                    content=content,
                )
            )
        return sections

    def _summarize_run_output(self, run: Run, sections: list[RunOutputSection]) -> dict[str, object]:
        headline = f"Attempt {run.attempt} is {run.status.value}."
        highlights: list[str] = []
        section_map = {section.label: section.content for section in sections}

        if run.summary.strip():
            highlights.append(run.summary.strip())

        report_content = section_map.get("report")
        if report_content:
            try:
                report_payload = json.loads(report_content)
                worker_outcome = str(report_payload.get("worker_outcome") or "").strip()
                failure_category = str(report_payload.get("failure_category") or "").strip()
                if worker_outcome:
                    highlights.append(f"Worker outcome: {worker_outcome}.")
                if failure_category:
                    highlights.append(f"Failure category: {failure_category}.")
            except json.JSONDecodeError:
                highlights.append("A structured report exists, but it could not be parsed cleanly.")

        for label in ("test output", "compile output", "worker stderr", "codex worker stderr", "llm stderr"):
            content = section_map.get(label)
            if content:
                highlights.append(f"{label.title()}: {self._truncate_text(content, 160)}")

        status_value = run.status.value
        if status_value in {"failed", "blocked"}:
            interpretation = "The latest implementation attempt did not complete cleanly. Review the evidence before deciding whether to retry or investigate."
            recommended_next = "Ask the harness to summarize the failure or open investigation mode if the process feels wrong."
        elif status_value in {"analyzing", "working"}:
            interpretation = "The latest attempt is still in progress or has not reached a final decision yet."
            recommended_next = "Review the current evidence and decide whether to wait, redirect the harness, or investigate."
        elif status_value in {"completed"}:
            interpretation = "The latest implementation step completed. Review the result to decide whether to continue to the next slice."
            recommended_next = "Ask the harness what changed or continue execution if the result matches your intent."
        else:
            interpretation = "The latest run produced evidence, but the state still needs human review."
            recommended_next = "Review the summary first, then inspect raw evidence only if something looks off."

        return {
            "headline": headline,
            "interpretation": interpretation,
            "recommended_next": recommended_next,
            "highlights": highlights[:4],
        }


class _EventBus:
    """Simple pub/sub for SSE.  Clients register a queue; writers broadcast."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Queue[str | None]] = []

    def subscribe(self) -> Queue[str | None]:
        q: Queue[str | None] = Queue(maxsize=32)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: Queue[str | None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event: str) -> None:
        with self._lock:
            dead: list[Queue[str | None]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            for q in dead:
                try:
                    self._subscribers.remove(q)
                except ValueError:
                    pass


class HarnessUIHandler(BaseHTTPRequestHandler):
    server_version = "AccruviaHarnessUI/0.1"

    @property
    def data_service(self) -> HarnessUIDataService:
        return self.server.data_service  # type: ignore[attr-defined]

    @property
    def event_bus(self) -> _EventBus:
        return self.server.event_bus  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(_INDEX_HTML)
            return
        if parsed.path == "/atomic":
            self._send_html(_ATOMIC_HTML)
            return
        if parsed.path == "/promotion-review":
            self._send_html(_PROMOTION_REVIEW_HTML)
            return
        if parsed.path == "/token-performance":
            self._send_html(_TOKEN_PERFORMANCE_HTML)
            return
        if parsed.path == "/settings":
            self._send_html(_SETTINGS_HTML)
            return
        if parsed.path == "/objectives/new":
            self._send_html(_OBJECTIVE_CREATE_HTML)
            return
        if parsed.path == "/workspace":
            self._send_html(_FULL_UI_HTML)
            return
        if parsed.path == "/harness":
            self._send_html(_HARNESS_HTML)
            return
        if parsed.path == "/app.js":
            self._send_text(_APP_JS, content_type="application/javascript; charset=utf-8")
            return
        if parsed.path == "/app.css":
            self._send_text(_APP_CSS, content_type="text/css; charset=utf-8")
            return
        if parsed.path == "/api/projects":
            self._send_json(self.data_service.list_projects())
            return
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/workspace"):
            project_ref = parsed.path[len("/api/projects/") : -len("/workspace")].strip("/")
            self._dispatch_json(lambda: self.data_service.project_workspace(project_ref))
            return
        if parsed.path == "/api/version":
            self._send_json({"commit": _GIT_COMMIT, "started_at": _SERVER_STARTED_AT})
            return
        if parsed.path == "/api/harness":
            self._send_json(self.data_service.harness_overview())
            return
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/intent"):
            self._send_json({"error": "Method not allowed"}, status=HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/cli-output"):
            run_id = parsed.path[len("/api/runs/") : -len("/cli-output")].strip("/")
            self._dispatch_json(lambda: self.data_service.run_cli_output(run_id))
            return
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/supervisor"):
            project_id = parsed.path[len("/api/projects/") : -len("/supervisor")].strip("/")
            self._send_json(self.data_service.supervisor_status(project_id))
            return
        if parsed.path == "/api/events":
            self._handle_sse()
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/repo-settings"):
            project_id = parsed.path[len("/api/projects/") : -len("/repo-settings")].strip("/")
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.update_project_repo_settings(
                    project_id,
                    promotion_mode=str(payload.get("promotion_mode") or ""),
                    repo_provider=str(payload.get("repo_provider") or ""),
                    repo_name=str(payload.get("repo_name") or ""),
                    base_branch=str(payload.get("base_branch") or ""),
                ),
                notify=True,
            )
            return
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/objectives"):
            project_ref = parsed.path[len("/api/projects/") : -len("/objectives")].strip("/")
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.create_objective(
                    project_ref,
                    str(payload.get("title") or ""),
                    str(payload.get("summary") or ""),
                ),
                status=HTTPStatus.CREATED,
                notify=True,
            )
            return
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/comments"):
            project_ref = parsed.path[len("/api/projects/") : -len("/comments")].strip("/")
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.add_operator_comment(
                    project_ref,
                    str(payload.get("text") or ""),
                    str(payload.get("author") or ""),
                    str(payload.get("objective_id") or "").strip() or None,
                ),
                status=HTTPStatus.CREATED,
                notify=True,
            )
            return
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/tasks"):
            objective_id = parsed.path[len("/api/objectives/") : -len("/tasks")].strip("/")
            self._dispatch_json(
                lambda: self.data_service.create_linked_task(objective_id),
                status=HTTPStatus.CREATED,
                notify=True,
            )
            return
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/interrogation"):
            objective_id = parsed.path[len("/api/objectives/") : -len("/interrogation")].strip("/")
            self._dispatch_json(
                lambda: self.data_service.complete_interrogation_review(objective_id),
                status=HTTPStatus.CREATED,
                notify=True,
            )
            return
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/promotion/force"):
            objective_id = parsed.path[len("/api/objectives/") : -len("/promotion/force")].strip("/")
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.force_promote_objective_review(
                    objective_id,
                    rationale=str(payload.get("rationale") or ""),
                    author=str(payload.get("author") or "operator"),
                ),
                status=HTTPStatus.CREATED,
                notify=True,
            )
            return
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/promote"):
            objective_id = parsed.path[len("/api/objectives/") : -len("/promote")].strip("/")
            self._dispatch_json(
                lambda: self.data_service.promote_objective_to_repo(objective_id),
                notify=True,
            )
            return
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/mermaid/proposal/accept"):
            objective_id = parsed.path[len("/api/objectives/") : -len("/mermaid/proposal/accept")].strip("/")
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.accept_mermaid_proposal(objective_id, str(payload.get("proposal_id") or "")),
                status=HTTPStatus.CREATED,
                notify=True,
            )
            return
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/mermaid/proposal/reject"):
            objective_id = parsed.path[len("/api/objectives/") : -len("/mermaid/proposal/reject")].strip("/")
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.reject_mermaid_proposal(
                    objective_id,
                    str(payload.get("proposal_id") or ""),
                    resolution=str(payload.get("resolution") or "refine"),
                ),
                status=HTTPStatus.CREATED,
                notify=True,
            )
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/run"):
            task_id = parsed.path[len("/api/tasks/") : -len("/run")].strip("/")
            self._dispatch_json(
                lambda: self.data_service.run_task(task_id),
                status=HTTPStatus.CREATED,
                notify=True,
            )
            return
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/supervise"):
            project_id = parsed.path[len("/api/projects/") : -len("/supervise")].strip("/")
            self._dispatch_json(
                lambda: self.data_service.start_supervisor(project_id),
                status=HTTPStatus.CREATED,
                notify=True,
            )
            return
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/supervise/stop"):
            project_id = parsed.path[len("/api/projects/") : -len("/supervise/stop")].strip("/")
            self._dispatch_json(
                lambda: self.data_service.stop_supervisor(project_id),
                notify=True,
            )
            return
        if parsed.path == "/api/cli/command":
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.run_cli_command(str(payload.get("command") or "")),
                status=HTTPStatus.CREATED,
                notify=True,
            )
            return
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/frustrations"):
            project_ref = parsed.path[len("/api/projects/") : -len("/frustrations")].strip("/")
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.add_operator_frustration(
                    project_ref,
                    str(payload.get("text") or ""),
                    str(payload.get("author") or ""),
                    str(payload.get("objective_id") or "").strip() or None,
                ),
                status=HTTPStatus.CREATED,
                notify=True,
            )
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/retry"):
            task_id = parsed.path[len("/api/tasks/") : -len("/retry")].strip("/")
            self._dispatch_json(
                lambda: self.data_service.retry_task(task_id),
                notify=True,
            )
            return
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/retry-failed"):
            project_id = parsed.path[len("/api/projects/") : -len("/retry-failed")].strip("/")
            self._dispatch_json(
                lambda: self.data_service.retry_all_failed(project_id),
                notify=True,
            )
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/mermaid"):
            objective_id = parsed.path[len("/api/objectives/") : -len("/mermaid")].strip("/")
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.update_mermaid_artifact(
                    objective_id,
                    status=str(payload.get("status") or ""),
                    summary=str(payload.get("summary") or ""),
                    blocking_reason=str(payload.get("blocking_reason") or ""),
                ),
                notify=True,
            )
            return
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/intent"):
            objective_id = parsed.path[len("/api/objectives/") : -len("/intent")].strip("/")
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.update_intent_model(
                    objective_id,
                    intent_summary=str(payload.get("intent_summary") or ""),
                    success_definition=str(payload.get("success_definition") or ""),
                    non_negotiables=list(payload.get("non_negotiables") or []),
                    frustration_signals=list(payload.get("frustration_signals") or []),
                ),
                notify=True,
            )
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, _format: str, *args) -> None:
        return

    def _dispatch_json(self, fn, *, status: HTTPStatus = HTTPStatus.OK, notify: bool = False) -> None:
        try:
            payload = fn()
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(payload, status=status)
        if notify:
            self.event_bus.publish("workspace-changed")

    def _read_json_body(self) -> dict[str, object]:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, payload: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_body(body)

    def _send_html(self, body: str) -> None:
        self._send_text(body, content_type="text/html; charset=utf-8")

    def _send_text(self, body: str, *, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self._write_body(encoded)

    def _handle_sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = self.event_bus.subscribe()
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                except Empty:
                    # Send keepalive comment
                    self.wfile.write(b":\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                self.wfile.write(f"data: {event}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.event_bus.unsubscribe(q)

    def _write_body(self, body: bytes) -> None:
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno not in {errno.EPIPE, errno.ECONNRESET}:
                raise
            return


def _verify_install_path() -> None:
    """Refuse to start if the installed package points outside the source tree."""
    import accruvia_harness
    installed = Path(accruvia_harness.__file__).resolve().parent
    expected = Path(__file__).resolve().parent
    if installed != expected:
        raise RuntimeError(
            f"Installed package points to {installed}, expected {expected}. "
            f"Run: pip install -e . from the project root."
        )


def start_ui_server(ctx, *, host: str, port: int, open_browser: bool, project_ref: str | None = None) -> None:
    _verify_install_path()
    # Wire the LLM availability gate into the engine if config is available.
    if hasattr(ctx, "config") and ctx.config is not None:
        from .llm_availability import LLMAvailabilityGate
        from .onboarding import probe_llm_command
        gate = LLMAvailabilityGate(
            probe_fn=probe_llm_command,
            commands=[
                ("codex", ctx.config.llm_codex_command or ""),
                ("claude", ctx.config.llm_claude_command or ""),
                ("command", ctx.config.llm_command or ""),
            ],
        )
        ctx.engine.set_llm_gate(gate)
    data_service = HarnessUIDataService(ctx)
    resolved_port = _resolve_ui_port(host, port)
    event_bus = _EventBus()
    server = ThreadingHTTPServer((host, resolved_port), HarnessUIHandler)
    server.data_service = data_service  # type: ignore[attr-defined]
    server.event_bus = event_bus  # type: ignore[attr-defined]
    url = f"http://{host}:{resolved_port}/"
    if project_ref:
        project_id = resolve_project_ref(ctx, project_ref)
        url = f"{url}?project_id={project_id}"
    if resolved_port != port:
        print(f"Port {port} is busy. Using {resolved_port} instead.", flush=True)
    print(f"Harness UI running at {url} (commit {_GIT_COMMIT})", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        print(f"Refresh your existing browser tab at {url}", flush=True)
    # Background thread polls for database changes and pushes SSE events.
    _stop_change_detector = threading.Event()

    def _detect_changes() -> None:
        last_signature: str | None = None
        while not _stop_change_detector.wait(timeout=3):
            try:
                tasks = data_service.store.list_tasks()
                records = data_service.store.list_context_records()
                recent_records = records[-20:]
                sig = ";".join(
                    f"{t.id}:{t.status.value}:{t.updated_at.isoformat()}" for t in tasks
                )
                sig += "|ctx:" + ";".join(
                    f"{r.id}:{r.record_type}:{r.created_at.isoformat()}" for r in recent_records
                )
                if last_signature is not None and sig != last_signature:
                    event_bus.publish("workspace-changed")
                last_signature = sig
            except Exception:
                pass

    change_thread = threading.Thread(target=_detect_changes, daemon=True)
    change_thread.start()

    _auto_start_supervisors(data_service, ctx)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_change_detector.set()
        for project in data_service.store.list_projects():
            _BACKGROUND_SUPERVISOR.stop(project.id)
        server.server_close()


def _auto_start_supervisors(data_service: HarnessUIDataService, ctx) -> None:
    """Start background supervisors for projects with pending tasks, and resume stalled atomic generation."""
    # A fresh server has zero workers — clear all leases unconditionally
    # so recover_stale_state can reset any active tasks from prior sessions.
    with data_service.store.connect() as connection:
        cleared = connection.execute("DELETE FROM task_leases").rowcount
    recovered = data_service.store.recover_stale_state()
    if cleared or any(int(count or 0) > 0 for count in recovered.values()):
        print(f"  Startup recovery: cleared {cleared} leases, recovered {recovered}", flush=True)
    for project in data_service.store.list_projects():
        # Resume any stalled atomic generation
        for objective in data_service.store.list_objectives(project.id):
            try:
                data_service._maybe_resume_atomic_generation(objective.id)
                data_service._maybe_resume_objective_review(objective.id)
            except Exception:
                pass
        # Start supervisor if there's work to do
        metrics = data_service.store.metrics_snapshot(project.id)
        pending = int(metrics.get("tasks_by_status", {}).get("pending", 0))
        active = int(metrics.get("tasks_by_status", {}).get("active", 0))
        if pending + active > 0:
            started = _BACKGROUND_SUPERVISOR.start(project.id, ctx.engine, watch=True)
            if started:
                print(f"  Auto-started harness for {project.name} ({pending} pending, {active} active)", flush=True)


def _resolve_ui_port(host: str, preferred_port: int) -> int:
    for port in range(preferred_port, preferred_port + 25):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError as exc:
                if exc.errno in {errno.EADDRINUSE, 48, 98}:
                    continue
                raise
            return port
    raise OSError(f"No free UI port found in range {preferred_port}-{preferred_port + 24}")
