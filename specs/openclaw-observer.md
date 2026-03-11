# OpenClaw Observer Spec

## Purpose

OpenClaw acts as a read-only observer over accruvia-harness state. It consumes structured evidence from the harness, maintains a rolling understanding of system behavior, and answers natural language questions from the operator via chat platforms (Telegram, Slack, etc).

## Architecture

```
Telegram / Slack / CLI
        |
        v
   OpenClaw Agent
   (LLM + memory + evidence cache)
        |
        v
   Harness Query API
   (read-only, structured JSON)
```

OpenClaw is not a wrapper for chat platforms. It is an interrogation agent that *happens to be reachable* through chat platforms. The chat transport is pluggable and secondary to the core capability: understanding and explaining harness behavior from evidence.

## How OpenClaw Watches

### Primary mechanism: on-demand query

When the operator asks a question, OpenClaw fetches fresh evidence from the harness and answers from it. This is the simplest and most useful mode.

```
Operator (Telegram): "what's stuck?"
OpenClaw -> harness: context-packet, ops-report
OpenClaw -> Operator: "Task X has been active for 4 hours with no new runs.
                       Last run failed with missing report artifact.
                       The decider chose RETRY but no retry has started."
```

**Implementation**: OpenClaw calls harness CLI commands or query service methods and receives JSON. It does not need persistent connections or streaming.

### Secondary mechanism: periodic digest

OpenClaw polls the harness on a configurable interval (e.g. every 15 minutes) and maintains a rolling summary. This lets it answer questions with context even when the operator hasn't asked recently.

```
Every 15 min:
  harness context-packet --project-id $PROJECT_ID
  harness ops-report --project-id $PROJECT_ID
  harness events --entity-type task  (since last poll)
  -> update internal evidence cache
```

**Purpose**: builds temporal awareness. OpenClaw can say "task X was pending 30 minutes ago but is now active" without the operator having to ask twice.

### Optional mechanism: event push

The harness emits a lightweight notification when significant state changes occur (task completed, task failed, promotion rejected, branch winner selected). OpenClaw receives these and can proactively notify the operator.

```
Harness -> webhook POST -> OpenClaw
  { "event_type": "task_failed", "task_id": "task_abc123", "summary": "..." }

OpenClaw -> Operator (Telegram): "Task 'Fix auth bug' just failed.
                                   All 3 retry attempts exhausted.
                                   Decider recommended branching."
```

**Implementation**: a post-event hook in the harness that POSTs to a configured URL. OpenClaw provides the endpoint. This is the only component that requires the harness to actively push data.

## What OpenClaw Consumes

All data comes from existing harness query surfaces. No new data model required.

| Query | Use | Freshness |
|---|---|---|
| `context-packet` | Portfolio-level understanding, top tasks, metrics | On-demand + periodic |
| `ops-report` | Backlog, pending affirmations, profile metrics | On-demand + periodic |
| `task-report <id>` | Deep dive on a specific task's evidence chain | On-demand |
| `lineage-report <id>` | Parent/child task relationships | On-demand |
| `events` | Recent state transitions, audit trail | Periodic + event push |
| `summary` | High-level counts and status | On-demand |
| `dashboard-report` | Queue depth, promotion rates, costs | On-demand |
| `telemetry-report` | Timing and performance data | On-demand |

## What OpenClaw Does NOT Do

- It does not create, modify, or delete any harness state.
- It does not trigger runs, retries, promotions, or branching.
- It does not have write access to the harness database.
- It does not make operational decisions. It reports and explains.

If the operator wants to act on what OpenClaw reports, they issue harness commands themselves. OpenClaw may suggest commands but does not execute them.

## Harness-Side Requirements

### 1. Query endpoint (required)

The harness must expose its query surfaces in a way OpenClaw can call. Options in order of simplicity:

**Option A: CLI invocation (simplest)**
OpenClaw shells out to `accruvia-harness <command>` and parses JSON stdout. Works immediately with zero harness changes.

**Option B: Unix socket / HTTP API (better for production)**
A lightweight read-only HTTP server that wraps `HarnessQueryService` methods. Returns JSON. Single process, no auth needed for local use.

```
GET /api/context-packet?project_id=...
GET /api/ops-report?project_id=...
GET /api/task-report/:task_id
GET /api/events?since=2026-03-10T00:00:00Z
```

**Recommendation**: start with Option A. Move to Option B only if CLI startup latency becomes annoying (currently ~0.3s per call, which is fine for chat-driven queries).

### 2. Event webhook (optional, for proactive notifications)

A post-commit hook on significant events that POSTs to a configured URL.

```python
# In HarnessConfig:
observer_webhook_url: str | None = None

# After recording an event:
if config.observer_webhook_url and event_type in NOTIFY_EVENT_TYPES:
    requests.post(config.observer_webhook_url, json=event_payload, timeout=5)
```

Notable event types to push:
- `task_completed`
- `task_failed`
- `promotion_rejected`
- `promotion_approved`
- `branch_winner_selected`
- `run_blocked`

Fire-and-forget. If the webhook fails, log and move on. The harness does not depend on the observer being available.

### 3. Read-only query facade (recommended)

Wrap `HarnessQueryService` in a read-only store connection to make the mutation prevention architectural rather than conventional.

```python
class ReadOnlyStore:
    """Wraps SQLiteHarnessStore with a read-only connection."""
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
```

## OpenClaw-Side Requirements

### 1. Evidence ingestion

OpenClaw must be able to:
- Call harness query surfaces (CLI or HTTP)
- Parse JSON responses
- Maintain a rolling evidence cache (last N snapshots)
- Diff snapshots to detect changes ("task X moved from active to completed")

### 2. Conversation context

OpenClaw maintains per-operator conversation state:
- Last few questions and answers
- Which tasks/projects the operator has been asking about (implicit focus)
- Recent evidence snapshots for temporal reasoning

### 3. Chat transport adapter

Telegram adapter (primary):
- Receives messages via Telegram Bot API
- Routes to OpenClaw agent
- Returns responses as Telegram messages
- Supports markdown formatting

The adapter is thin. It handles message transport only. All intelligence is in the OpenClaw agent.

### 4. Proactive notification routing

When the harness pushes an event via webhook:
- OpenClaw evaluates whether it's worth notifying the operator
- Applies configurable filters (e.g. only notify on failures, not routine completions)
- Sends a Telegram message if the event passes the filter

## Example Conversations

```
Operator: what's the backlog look like?
OpenClaw: 3 projects active. 12 tasks pending across all projects.
          Project "auth-service" has 5 pending, 2 active (at concurrency limit).
          Project "frontend" has 4 pending, none active.
          No blocked tasks. 1 promotion pending affirmation.

Operator: which promotion?
OpenClaw: Task "Refactor session handler" (task_8f2a...) in auth-service.
          Run 3 completed with all required artifacts.
          Promotion status: pending. Validator passed generic checks.
          Awaiting LLM affirmation.

Operator: what happened to the terraform tasks?
OpenClaw: Last terraform-profile task was "Migrate VPC config" (task_3b1c...).
          Failed after 3 attempts. Each run produced a plan but no valid
          terraform output. Last decision: FAIL (retry budget exhausted,
          max_branches=1 so no branching). Completed 2 hours ago.

[Proactive notification]
OpenClaw: Task "Fix payment webhook" just completed successfully.
          Run 2 produced plan + report. Evaluation confidence: 0.92.
          Decision: PROMOTE. Ready for promotion review.
```

## Implementation Order

1. **CLI query integration** — OpenClaw calls `accruvia-harness` CLI commands and parses JSON. Zero harness changes. Gets the core value immediately.

2. **Telegram adapter** — thin bot that routes messages to/from OpenClaw. Standard Telegram Bot API.

3. **Periodic digest** — OpenClaw polls harness every N minutes, caches evidence, enables temporal reasoning.

4. **Event webhook** — add `observer_webhook_url` config to harness, POST on significant events. Enables proactive notifications.

5. **Read-only HTTP API** — only if CLI latency becomes a problem or if OpenClaw needs to run on a different machine.

## Configuration

Harness side (`~/.accruvia-harness/config` or env vars):
```
ACCRUVIA_HARNESS_OBSERVER_WEBHOOK_URL=http://localhost:8900/events
ACCRUVIA_HARNESS_NOTIFY_EVENTS=task_completed,task_failed,promotion_rejected,branch_winner_selected
```

OpenClaw side:
```
OPENCLAW_HARNESS_CLI=accruvia-harness
OPENCLAW_HARNESS_DB=/path/to/harness.db  # for read-only direct access (optional)
OPENCLAW_POLL_INTERVAL_SECONDS=900
OPENCLAW_TELEGRAM_BOT_TOKEN=...
OPENCLAW_TELEGRAM_CHAT_ID=...
OPENCLAW_NOTIFY_FILTER=failures_only|all|none
```
