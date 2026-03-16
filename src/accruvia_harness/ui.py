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
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .commands.common import resolve_project_ref
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

body[data-view="control-flow"] .app-shell {
  grid-template-columns: 1fr;
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

body[data-view="control-flow"] .content {
  display: grid;
  grid-template-columns: minmax(420px, 0.95fr) minmax(520px, 1.05fr);
  align-items: start;
  gap: 0;
  min-height: 100vh;
  padding: 0;
  background: #ffffff;
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

body[data-view="atomic"] .app-shell {
  grid-template-columns: 1fr;
}

body[data-view="atomic"] .sidebar,
body[data-view="atomic"] .header,
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

body[data-view="atomic"] .content {
  display: grid;
  grid-template-columns: minmax(420px, 0.95fr) minmax(520px, 1.05fr);
  align-items: start;
  gap: 0;
  min-height: 100vh;
  padding: 0;
  background: #ffffff;
}

body[data-view="atomic"] #next-action-panel {
  display: block !important;
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
  width: 100%;
  margin: 0;
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

body[data-view="atomic"] .atomic-objective-picker {
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

.atomic-generation-meta .pill {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0.24rem 0.6rem;
  background: #fffdf8;
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

.atomic-list {
  display: grid;
  gap: 0.85rem;
}

.atomic-card {
  border: 1px solid var(--line);
  border-radius: 1rem;
  padding: 0.9rem;
  background: #fffdf8;
}

.atomic-card.active {
  border-color: var(--accent);
  box-shadow: 0 8px 24px rgba(162, 76, 43, 0.08);
}

.atomic-card .title {
  font-weight: 700;
  margin-bottom: 0.35rem;
}

.atomic-card .meta {
  color: var(--muted);
  font-size: 0.88rem;
  margin-bottom: 0.45rem;
}

.atomic-card .body {
  white-space: pre-wrap;
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
  opacity: 0.75;
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


_APP_JS = """
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  securityLevel: 'loose',
});

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
};

const appShell = document.getElementById('app-shell');
const content = document.querySelector('.content');
const sidebarToggle = document.getElementById('sidebar-toggle');
const sidebarToggleLabel = document.getElementById('sidebar-toggle-label');
const projectSelect = document.getElementById('project-select');
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
const diagramShell = document.getElementById('diagram-shell');
const outputTabs = document.getElementById('output-tabs');
const outputBody = document.getElementById('output-body');
const pageError = document.getElementById('page-error');
let activeConversationController = null;
let selectedDiagramElement = null;

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
  state.objectiveId = value;
  if (value) {
    localStorage.setItem('accruvia.ui.objectiveId', value);
  } else {
    localStorage.removeItem('accruvia.ui.objectiveId');
  }
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
          `<div class="transcript-bubble ${item.role === 'operator' ? 'operator' : item.role === 'system' ? 'system' : 'harness'}"><div class="meta">${escapeHtml(item.label)}</div><div>${escapeHtml(item.text)}</div></div>`
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
        `<div class="transcript-bubble ${item.role === 'operator' ? 'operator' : item.role === 'system' ? 'system' : 'harness'}"><div class="meta">${escapeHtml(item.label)}</div><div>${escapeHtml(item.text)}</div></div>`
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
  projectSelect.innerHTML = '';
  for (const project of state.projects) {
    const option = document.createElement('option');
    option.value = project.id;
    option.textContent = `${project.name} (${project.id})`;
    option.selected = project.id === state.projectId;
    projectSelect.appendChild(option);
  }
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
      done: 'Done when you click Matches my flow or Doesn\\'t match yet.',
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
  const linkedTasks = Array.isArray(objective.atomic_units) ? objective.atomic_units : [];
  atomicTitle.textContent = 'Atomic units of work';
  atomicSummary.textContent = linkedTasks.length
    ? 'These atomic units were derived from the accepted flowchart for this objective. Use the CLI to clarify or challenge the decomposition.'
    : 'Atomic units will appear here as the harness derives them from the accepted flowchart.';
  if (generation.status === 'running') {
    const dots = '.'.repeat((Math.floor(Date.now() / 500) % 3) + 1);
    atomicGenerationStatus.textContent = `Generating atomic units from Mermaid v${generation.diagram_version}${dots} ${generation.unit_count || 0} ready so far.`;
  } else if (generation.status === 'completed') {
    atomicGenerationStatus.textContent = `Atomic generation complete for Mermaid v${generation.diagram_version}. ${generation.unit_count || 0} units ready.`;
  } else if (generation.status === 'failed') {
    atomicGenerationStatus.textContent = generation.error || 'Atomic generation failed.';
  } else {
    atomicGenerationStatus.textContent = 'No atomic generation is currently running.';
  }
  const lastActivity = generation.last_activity_at ? formatRelativeTime(generation.last_activity_at) : '';
  const phase = generation.phase || '';
  const pills = [];
  if (phase) {
    const klass = generation.status === 'running' ? 'pill live' : 'pill';
    pills.push(`<span class="${klass}">Phase: ${escapeHtml(phase)}</span>`);
  }
  if (lastActivity) {
    pills.push(`<span class="pill">Last activity ${escapeHtml(lastActivity)}</span>`);
  }
  if (generation.status === 'running' && generation.unit_count) {
    pills.push(`<span class="pill">${escapeHtml(String(generation.unit_count))} published</span>`);
  }
  atomicGenerationMeta.innerHTML = pills.join('');
  if (!linkedTasks.length) {
    atomicList.innerHTML = '<div class="empty">No atomic units yet.</div>';
    return;
  }
  atomicList.innerHTML = linkedTasks.map((task) => {
    const latestRun = task.latest_run || null;
    const meta = [
      `Status: ${escapeHtml(task.status)}`,
      task.strategy ? `Strategy: ${escapeHtml(task.strategy)}` : '',
      latestRun ? `Latest run: attempt ${latestRun.attempt} · ${escapeHtml(latestRun.status)}` : 'No run yet',
    ].filter(Boolean).join(' · ');
    return `
      <div class="atomic-card ${task.id === state.taskId ? 'active' : ''}" data-atomic-task="${task.id}">
        <div class="title">${escapeHtml(task.title)}</div>
        <div class="meta">${meta}</div>
        <div class="body">${escapeHtml(task.objective || 'No task objective recorded.')}${task.rationale ? `\n\nWhy this unit exists: ${escapeHtml(task.rationale)}` : ''}</div>
      </div>
    `;
  }).join('');
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
    const id = `diagram-${Math.random().toString(36).slice(2)}`;
    const rendered = await mermaid.render(id, code);
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
  workspaceTitle.textContent = objective ? objective.title : workspace.project.name;
  workspaceSummary.textContent = objective
    ? (objective.summary || 'No objective summary recorded.')
    : (workspace.project.description || 'No project summary recorded.');
  workspaceStatus.textContent = `${workspace.loop_status.status} · queue ${workspace.loop_status.queue_depth}`;
}

async function loadProjects() {
  const payload = await api('/api/projects');
  state.projects = payload.projects;
  const preferredProjectId = new URLSearchParams(window.location.search).get('project_id');
  if (preferredProjectId && state.projects.some((project) => project.id === preferredProjectId)) {
    setProjectId(preferredProjectId);
  }
  if (!state.projectId && state.projects.length > 0) {
    setProjectId(state.projects[0].id);
  }
  if (state.projectId && !state.projects.some((project) => project.id === state.projectId)) {
    setProjectId(state.projects[0]?.id || null);
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
  renderTasks();
  renderRuns();
  renderExecutionPanel();
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

createObjectiveForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!state.projectId) return;
  const title = createObjectiveTitle.value.trim();
  if (!title) return;
  try {
    clearError();
    const payload = await api(`/api/projects/${encodeURIComponent(state.projectId)}/objectives`, {
      method: 'POST',
      body: JSON.stringify({
        title,
        summary: createObjectiveSummary.value.trim(),
      }),
    });
    createObjectiveTitle.value = '';
    createObjectiveSummary.value = '';
    setObjectiveId(payload.objective.id);
    await loadWorkspace();
  } catch (error) {
    showError(error.message || 'Unable to create objective');
  }
});

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

conversationInterrupt.addEventListener('click', () => {
  if (activeConversationController) {
    activeConversationController.abort();
  }
});

if (diagramCommentAnchorClear) {
  diagramCommentAnchorClear.addEventListener('click', () => {
    clearDiagramAnchor();
    conversationInput.focus();
  });
}

diagramShell.addEventListener('click', (event) => {
  if (state.diagramPan.isDragging) {
    state.diagramPan.isDragging = false;
    diagramShell.classList.remove('panning');
    return;
  }
  const objective = currentObjective();
  if (!objective || currentFocusMode(objective) !== 'mermaid_review') return;
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
        window.location.assign('/atomic');
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
      window.location.assign('/atomic');
    }
  } catch (error) {
    showError(error.message || 'Unable to update Mermaid review state');
  }
}

mermaidControls.addEventListener('click', async (event) => {
  const action = event.target?.dataset?.mermaidAction;
  await handleMermaidAction(action);
});

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

executionPrimaryButton.addEventListener('click', async () => {
  try {
    await handleExecutionPrimaryAction();
  } catch (error) {
    state.suppressFocusAnimation = false;
    showError(error.message || 'Unable to continue execution from the UI');
  }
});

conversationPrimaryButton.addEventListener('click', async () => {
  try {
    await handleExecutionPrimaryAction();
  } catch (error) {
    state.suppressFocusAnimation = false;
    showError(error.message || 'Unable to continue execution from the UI');
  }
});

inlineOutputToggle.addEventListener('click', () => {
  const hidden = inlineOutputBody.hidden;
  inlineOutputBody.hidden = !hidden;
  inlineOutputTabs.hidden = !hidden;
  inlineOutputToggle.textContent = hidden ? 'Hide raw evidence' : 'Show raw evidence';
});

projectSelect.addEventListener('change', async () => {
  setProjectId(projectSelect.value);
  setObjectiveId(null);
  state.taskId = null;
  state.runId = null;
  state.manualFocusMode = null;
  state.showInlineReview = false;
  setSidebarCollapsed(true);
  await loadWorkspace();
});

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
  atomicList.addEventListener('click', (event) => {
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

function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

async function main() {
  try {
    applySidebarState();
    await loadProjects();
    await loadWorkspace();
    window.setInterval(() => {
      renderMermaidMeta(currentObjective());
      if (state.view === 'atomic') {
        renderAtomicUnits();
      }
    }, 1000);
    window.setInterval(async () => {
      try {
        const objective = currentObjective();
        if (state.view === 'atomic' && objective?.atomic_generation?.status === 'running') {
          await loadWorkspace();
        }
      } catch (_error) {
        // Ignore polling failures; the next poll can recover.
      }
    }, 2000);
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
  <body data-view="default">
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
          <section id="atomic-panel" class="panel" hidden>
            <h3 id="atomic-title">Atomic units of work</h3>
            <p id="atomic-summary" class="hint"></p>
            <p id="atomic-generation-status" class="hint"></p>
            <div id="atomic-generation-meta" class="atomic-generation-meta"></div>
            <div id="atomic-list" class="atomic-list"></div>
          </section>
          <section id="cli-panel" class="panel">
            <h3>CLI Output</h3>
            <p class="hint">Readable text artifacts from the selected run directory.</p>
            <div id="output-tabs" class="output-tabs"></div>
            <div id="output-body" class="output-body"></div>
          </section>
        </div>
      </main>
    </div>
    <script type="module" src="/app.js"></script>
  </body>
</html>
"""

_INDEX_HTML = _FULL_UI_HTML.replace('data-view="default"', 'data-view="control-flow"', 1)
_ATOMIC_HTML = _FULL_UI_HTML.replace('data-view="default"', 'data-view="atomic"', 1)


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

    def project_workspace(self, project_ref: str) -> dict[str, object]:
        project_id = resolve_project_ref(self.ctx, project_ref)
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_ref}")
        objectives = self.store.list_objectives(project.id)
        tasks = self.store.list_tasks(project.id)
        task_payload = []
        latest_runs_by_task: dict[str, list[Any]] = {}
        for task in tasks:
            runs = self.store.list_runs(task.id)
            latest_runs_by_task[task.id] = runs
            task_payload.append(
                {
                    **serialize_dataclass(task),
                    "runs": [serialize_dataclass(run) for run in runs],
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
            units = self._derive_atomic_units(objective_id)
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

    def _derive_atomic_units(self, objective_id: str) -> list[dict[str, str]]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return []
        intent_model = self.store.latest_intent_model(objective_id)
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        comments = self.store.list_context_records(objective_id=objective_id, record_type="operator_comment")[-12:]
        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is not None and getattr(llm_router, "executors", {}):
            run_dir = self.workspace_root / "ui_atomic" / objective_id / new_id("generation")
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt = (
                "You are deriving atomic units of work from an accepted Mermaid flowchart.\n"
                "Return JSON only with one key: units.\n"
                "units must be an array of 3 to 7 objects with keys: title, objective, rationale, strategy.\n"
                "Each unit must be atomic, reviewable, and directly map to the accepted control flow.\n"
                "Do not restate the whole objective. Do not include duplicate or overlapping units.\n\n"
                f"Objective title: {objective.title}\n"
                f"Objective summary: {objective.summary}\n"
                f"Intent summary: {intent_model.intent_summary if intent_model else ''}\n"
                f"Success definition: {intent_model.success_definition if intent_model else ''}\n"
                f"Non-negotiables: {json.dumps(intent_model.non_negotiables if intent_model else [])}\n"
                f"Accepted Mermaid:\n{mermaid.content if mermaid else ''}\n"
                f"Recent operator comments: {json.dumps([record.content for record in comments], indent=2)}\n"
            )
            task = Task(
                id=new_id("ui_atomic_task"),
                project_id=objective.project_id,
                objective_id=objective.id,
                title=f"Generate atomic units for {objective.title}",
                objective="Derive atomic units from accepted Mermaid.",
                strategy="ui_atomic_generation",
                status=TaskStatus.COMPLETED,
            )
            run = Run(
                id=new_id("ui_atomic_run"),
                task_id=task.id,
                status=RunStatus.COMPLETED,
                attempt=1,
                summary=f"Atomic generation for {objective.id}",
            )
            try:
                result, _backend = llm_router.execute(LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir))
                parsed = json.loads(result.response_text.strip())
                units_raw = list(parsed.get("units") or [])
                units: list[dict[str, str]] = []
                for item in units_raw:
                    title = str(item.get("title") or "").strip()
                    objective_text = str(item.get("objective") or "").strip()
                    if not title or not objective_text:
                        continue
                    units.append(
                        {
                            "title": title,
                            "objective": objective_text,
                            "rationale": str(item.get("rationale") or "").strip(),
                            "strategy": str(item.get("strategy") or "atomic_from_mermaid").strip() or "atomic_from_mermaid",
                        }
                    )
                if units:
                    return units
            except Exception:
                pass

        labels = re.findall(r"\[(.*?)\]", mermaid.content if mermaid else "")
        units: list[dict[str, str]] = []
        seen: set[str] = set()
        for label in labels:
            cleaned = " ".join(label.split()).strip()
            if not cleaned:
                continue
            lowered = cleaned.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            if lowered in {
                "objective intake",
                "state aligned with intent",
                "operator approval",
                "review diagram in ui",
            }:
                continue
            units.append(
                {
                    "title": f"{cleaned}",
                    "objective": f"Implement and validate the '{cleaned}' stage so it behaves as described by the accepted flowchart.",
                    "rationale": f"This unit maps directly to the '{cleaned}' node in the accepted Mermaid.",
                    "strategy": "atomic_from_mermaid",
                }
            )
            if len(units) >= 6:
                break
        if not units:
            units.append(
                {
                    "title": f"Atomic work for {objective.title}",
                    "objective": "Derive the first reviewable implementation unit from the accepted flowchart.",
                    "rationale": "Fallback atomic unit because no specific Mermaid nodes were available.",
                    "strategy": "atomic_from_mermaid",
                }
            )
        return units

    def run_task(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        run = self.ctx.engine.run_once(task.id)
        return {"run": serialize_dataclass(run)}

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
            proposal = self.propose_mermaid_update(objective_id, directive=body)
            if proposal is not None:
                responder_result.reply = (
                    responder_result.reply.rstrip()
                    + "\n\nI generated a proposed Mermaid update from your instruction. Review the proposed diagram and choose whether to accept the proposed flowchart or rewind hard."
                )
                responder_result.recommended_action = "review_mermaid"
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="action_receipt",
                        project_id=project.id,
                        objective_id=objective_id,
                        visibility="operator_visible",
                        author_type="system",
                        content="Action receipt: Mermaid proposal generated.",
                        metadata={"kind": "mermaid_update", "status": "proposal_generated", "proposal_id": proposal["id"]},
                    )
                )
            else:
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="action_receipt",
                        project_id=project.id,
                        objective_id=objective_id,
                        visibility="operator_visible",
                        author_type="system",
                        content="Action receipt: Mermaid update was requested but no proposal was generated.",
                        metadata={"kind": "mermaid_update", "status": "not_applied"},
                    )
                )
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
            receipts.append(
                {
                    "id": record.id,
                    "objective_id": record.objective_id,
                    "text": record.content,
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
        for record in records:
            task_id = str(record.metadata.get("task_id") or "")
            task = tasks_by_id.get(task_id)
            runs = task_runs.get(task_id, [])
            latest_run = runs[-1] if runs else None
            units.append(
                {
                    "id": task_id or record.id,
                    "title": str(record.metadata.get("title") or (task.title if task else record.content)),
                    "objective": str(record.metadata.get("objective") or (task.objective if task else "")),
                    "rationale": str(record.metadata.get("rationale") or ""),
                    "strategy": str(record.metadata.get("strategy") or (task.strategy if task else "")),
                    "status": task.status.value if task is not None else "pending",
                    "order": int(record.metadata.get("order") or 0),
                    "latest_run": (
                        {
                            "attempt": latest_run.attempt,
                            "status": latest_run.status.value,
                        }
                        if latest_run is not None
                        else None
                    ),
                }
            )
        return sorted(units, key=lambda item: (int(item["order"]), str(item["title"])))

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


class HarnessUIHandler(BaseHTTPRequestHandler):
    server_version = "AccruviaHarnessUI/0.1"

    @property
    def data_service(self) -> HarnessUIDataService:
        return self.server.data_service  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(_INDEX_HTML)
            return
        if parsed.path == "/atomic":
            self._send_html(_ATOMIC_HTML)
            return
        if parsed.path == "/workspace":
            self._send_html(_FULL_UI_HTML)
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
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/intent"):
            self._send_json({"error": "Method not allowed"}, status=HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/cli-output"):
            run_id = parsed.path[len("/api/runs/") : -len("/cli-output")].strip("/")
            self._dispatch_json(lambda: self.data_service.run_cli_output(run_id))
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
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
            )
            return
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/tasks"):
            objective_id = parsed.path[len("/api/objectives/") : -len("/tasks")].strip("/")
            self._dispatch_json(
                lambda: self.data_service.create_linked_task(objective_id),
                status=HTTPStatus.CREATED,
            )
            return
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/interrogation"):
            objective_id = parsed.path[len("/api/objectives/") : -len("/interrogation")].strip("/")
            self._dispatch_json(
                lambda: self.data_service.complete_interrogation_review(objective_id),
                status=HTTPStatus.CREATED,
            )
            return
        if parsed.path.startswith("/api/objectives/") and parsed.path.endswith("/mermaid/proposal/accept"):
            objective_id = parsed.path[len("/api/objectives/") : -len("/mermaid/proposal/accept")].strip("/")
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.accept_mermaid_proposal(objective_id, str(payload.get("proposal_id") or "")),
                status=HTTPStatus.CREATED,
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
            )
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/run"):
            task_id = parsed.path[len("/api/tasks/") : -len("/run")].strip("/")
            self._dispatch_json(
                lambda: self.data_service.run_task(task_id),
                status=HTTPStatus.CREATED,
            )
            return
        if parsed.path == "/api/cli/command":
            payload = self._read_json_body()
            self._dispatch_json(
                lambda: self.data_service.run_cli_command(str(payload.get("command") or "")),
                status=HTTPStatus.CREATED,
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
                )
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
                )
            )
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, _format: str, *args) -> None:
        return

    def _dispatch_json(self, fn, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        try:
            payload = fn()
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(payload, status=status)

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

    def _write_body(self, body: bytes) -> None:
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno not in {errno.EPIPE, errno.ECONNRESET}:
                raise
            return


def start_ui_server(ctx, *, host: str, port: int, open_browser: bool, project_ref: str | None = None) -> None:
    data_service = HarnessUIDataService(ctx)
    resolved_port = _resolve_ui_port(host, port)
    server = ThreadingHTTPServer((host, resolved_port), HarnessUIHandler)
    server.data_service = data_service  # type: ignore[attr-defined]
    url = f"http://{host}:{resolved_port}/"
    if project_ref:
        project_id = resolve_project_ref(ctx, project_ref)
        url = f"{url}?project_id={project_id}"
    if resolved_port != port:
        print(f"Port {port} is busy. Using {resolved_port} instead.", flush=True)
    print(f"Harness UI running at {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        print(f"Refresh your existing browser tab at {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


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
