# Control Plane Plan

## Status

`V1 FROZEN FOR IMPLEMENTATION`

This document is frozen as the v1 planning baseline. Further changes should be treated as exceptions and made only if implementation reveals a concrete blocker or contradiction. Execution details, task breakdown, and sequencing belong in a separate checklist document rather than continuing to expand this plan.

## Purpose

Build a harness-native self-driver inside `accruvia-harness` that:

- keeps the API and harness alive
- runs coding work continuously
- avoids runaway loops
- merges safely to `main`
- uses durable breadcrumbs instead of chat memory as orchestration truth
- exposes Telegram as a coarse control and status surface only

This plan is intentionally implementation-oriented. It is a v1 build spec, not a long-term architecture wishlist.

## Can The Plan Improve?

Yes.

The prior version was still too broad for v1. The main improvements folded into this file are:

- `sa-planner` is deferred until the execution path is proven stable
- the event model is reduced to the minimum useful set
- breadcrumb format is reduced to a strict minimal handoff bundle
- direct-to-main rollback rules are made explicit
- `objective_stalled` is split into task/objective/promotion thresholds
- a `no_progress` rule is added so “successful but useless” loops are suppressed
- Telegram command semantics and `status` shape are made deterministic

The plan can still improve further. The additional improvements folded into this revision are:

- explicit v1 non-goals are added to reduce scope creep
- `sa-watch` is narrowed so deterministic recovery stays in code, not in the agent
- post-merge validation and rollback triggers are clarified further
- Telegram `status` is treated as deterministic operational data, not model narration
- a short implementation checklist is added for each build phase

## What Is Still Wrong With The Plan?

Several things are still weak or underspecified.

### 1. It is still slightly too architectural

The plan is much better than before, but some sections still describe intent more than implementation. The next work after this file should be schema, migrations, and executable contracts rather than more design prose.

### 2. Recovery policy is still too generic

The plan says the control plane should restart or pause things, but it still does not define exact retry counts, exact cooldown durations per failure class, or exact liveness thresholds per component. Those values must be encoded in code and tests, not left to interpretation.

### 3. Merge execution ownership is still fuzzy

The plan says the coding worker may not merge directly, but it does not yet name the concrete deterministic component that executes the merge after gates pass. V1 should make this explicit: the control plane performs the final merge action after reading gate results, not the worker.

### 4. Operator override is missing

The plan has Telegram commands for `freeze`, `thaw`, and pause/resume, but it does not define an operator override path for edge cases like:

- allow one additional retry
- skip a flaky non-critical check
- suppress a noisy alert temporarily

V1 should define whether these are intentionally unsupported or supported with explicit commands. Right now it is ambiguous.

### 5. Budget policy is still too thin

The expensive/cheap classes are defined, but the actual budget windows are still placeholders beyond a few defaults. That is acceptable for v1 only if the implementation hard-codes a minimal safe budget table. Otherwise cost control will drift immediately.

### 6. No explicit acceptance tests per subsystem

The phase checklist is helpful, but the plan still needs named acceptance tests for:

- API-down recovery
- harness-down recovery
- hung worker classification
- provider-rate-limit cooldown
- merge gate failure
- post-merge rollback
- no-progress freeze

Without those, it will be too easy to “implement” the system without proving the behavior actually works.

### 7. `sa-watch` still risks becoming a junk drawer

The boundary is clearer now, but it can still become the place where everything unclear gets dumped. The implementation should aggressively bias toward deterministic control-plane rules first, and treat `sa-watch` as a last resort for structural judgment only.

### 8. V1 should explicitly avoid planner logic

The file says `sa-planner` is deferred, but some surrounding text still frames the architecture as if planner automation is near-term. V1 should be judged successful without any planner at all.

### 9. The status surface should avoid LLM dependence entirely in v1

The plan still allows a model explanation layer for Telegram status. That is probably unnecessary. V1 should return deterministic structured status only, possibly rendered through templates, to avoid spending tokens on routine status requests.

### 10. Breadcrumb retention policy is missing

The plan defines breadcrumb contents, but not when old bundles are pruned, compacted, or archived. Even a simple retention rule is needed or the artifact store will become another form of unbounded memory.

## V1 Scope

### In V1

- deterministic control plane
- API health checks
- harness health checks
- minimal event store
- minimal breadcrumb store and index
- one coding worker
- failure classifier
- direct-to-main merge gates
- post-merge rollback
- `sa-watch`
- Telegram control/reporting

### Deferred

- `sa-planner`
- true parallelism
- Temporal integration
- rich budgeting
- advanced artifact cleanup
- multiple worker classes beyond the first coding lane

### Explicit V1 Non-Goals

- no attempt to make Telegram a general-purpose operator shell
- no long-form memory system
- no PR-based promotion path
- no autonomous objective generation
- no multi-worker parallel coding
- no attempt to infer workflow truth from chat transcripts
- no LLM-generated status prose for routine health checks

## Source Of Truth

Use the existing harness SQLite DB as the canonical metadata/state store.

### Store In SQLite

- global control state
- lane state
- events
- cooldowns
- budgets
- worker runs
- breadcrumb index
- recovery actions

### Store On Disk

- breadcrumb bundles
- raw evidence artifacts
- short summaries

SQLite is the source of truth. Filesystem is durable evidence storage.

## Repo Placement

Implement this inside the `accruvia-harness` repo.

Suggested module layout:

- `src/accruvia_harness/control_plane.py`
- `src/accruvia_harness/control_events.py`
- `src/accruvia_harness/control_models.py`
- `src/accruvia_harness/control_triggers.py`
- `src/accruvia_harness/control_breadcrumbs.py`
- `src/accruvia_harness/control_telegram.py`
- `src/accruvia_harness/control_classifier.py`
- `src/accruvia_harness/control_watch.py`

Suggested artifact layout:

- `.accruvia-harness/control/breadcrumbs/`
- `.accruvia-harness/control/journal/`

## OpenClaw Boundary

OpenClaw is not the orchestrator.

If retained, it is only:

- Telegram transport
- status surface
- coarse control surface

OpenClaw should not own:

- orchestration
- memory
- workflow truth
- retry decisions
- health classification

## Always-On Components

- `control-plane`
- `api-service`
- `harness-service`
- `telegram-reporter`

## V1 Agents

- `sa-watch`
- `coding-worker`
- `failure-classifier`

That is enough for v1.

## Model Assignments

| Component | Purpose | Default | Fallback |
|---|---|---|---|
| `control-plane` | scheduling, routing, cooldowns, health policy | none | none |
| `telegram-reporter` | status and coarse controls only | `Gemini 2.5 Flash` | deterministic templates |
| `failure-classifier` | normalize timeout, outage, rate-limit, hung process | `Gemini 2.5 Flash` | `Gemini 3.1 Pro` |
| `sa-watch` | catastrophic intervention only | `Codex` | `Gemini 3.1 Pro` |
| `coding-worker` | actual coding work | `Claude Code` | `Opus 4.6` |

## Principle

Use deterministic logic for ordinary recovery.

Use agents only where judgment is actually needed.

### Deterministic Control Plane Handles

- restart
- retry suppression
- backoff
- cooldown
- lane pause/resume
- merge gate enforcement
- rollback

### Agents Handle

- classification when deterministic parsing is insufficient
- catastrophic structural intervention
- coding work

## Hard Constraints

These are not suggestions. They are the rules that keep v1 from drifting into unnecessary complexity.

### Control Plane Constraints

- the control plane is deterministic and owns orchestration truth
- the control plane owns retry policy, cooldowns, lane state, and merge authority
- the control plane must not depend on chat transcripts to decide what to do next

### Telegram Constraints

- Telegram is a coarse control and status surface only
- Telegram must support only the declared fixed command set
- Telegram must not become a general operator shell in v1
- routine status responses should be deterministic and template-driven

### SA-Watch Constraints

- `sa-watch` is not on the hot path for ordinary recovery
- `sa-watch` is invoked only after deterministic recovery or classification is insufficient
- `sa-watch` must not create routine improvement work in v1
- `sa-watch` should prefer pause, freeze, suppress, or restart over generating new work

### Coding Worker Constraints

- the coding worker may implement changes and produce evidence
- the coding worker may not own retry policy
- the coding worker may not choose its own fallback policy
- the coding worker may not merge directly to `main`
- the coding worker must operate from a bounded worker packet, not inherited chat context

### Breadcrumb Constraints

- breadcrumb bundles are for evidence and handoff, not memory
- breadcrumb bundles must remain small and structured
- breadcrumb bundles may contain only the declared minimal files in v1
- breadcrumb bundles must not contain chain-of-thought or long narrative reasoning

### Scope Constraints

- `sa-planner` is out of scope for v1
- parallel coding execution is out of scope for v1
- PR-based promotion is out of scope for v1
- broad OpenClaw orchestration is out of scope for v1

### Merge Constraints

- direct-to-main merge is allowed only through deterministic control-plane merge execution
- all declared merge gates must pass before merge
- post-merge rollback must remain enabled in v1

### Exception Constraints

- do not add one-off exceptions to retry, merge, or Telegram command policy in v1
- if an exception seems necessary, change the policy explicitly and test it rather than patching around it ad hoc

### SA-Watch Boundary

`sa-watch` should not be on the hot path for ordinary process recovery.

Use this rule:

- ordinary restart, cooldown, retry suppression, and lane pause logic belongs to the control plane
- `sa-watch` is invoked only when deterministic recovery fails, classification remains uncertain, or the failure suggests a structural process defect

## Global And Lane States

### Global States

- `OFF`
- `STARTING`
- `HEALTHY`
- `DEGRADED`
- `FROZEN`

### Lane States

For `api`, `harness`, `worker`, `watch`, and `telegram`:

- `RUNNING`
- `PAUSED`
- `COOLDOWN`
- `DISABLED`

## Control Plane Schema

### `control_system_state`

- `id` TEXT PRIMARY KEY
- `global_state` TEXT NOT NULL
- `master_switch` INTEGER NOT NULL
- `freeze_reason` TEXT
- `updated_at` TEXT NOT NULL

Single row: `id = 'system'`

### `control_lane_state`

- `lane_name` TEXT PRIMARY KEY
- `state` TEXT NOT NULL
- `reason` TEXT
- `cooldown_until` TEXT
- `updated_at` TEXT NOT NULL

### `control_events`

- `id` TEXT PRIMARY KEY
- `event_type` TEXT NOT NULL
- `entity_type` TEXT NOT NULL
- `entity_id` TEXT NOT NULL
- `producer` TEXT NOT NULL
- `payload_json` TEXT NOT NULL
- `idempotency_key` TEXT NOT NULL UNIQUE
- `created_at` TEXT NOT NULL

### `control_cooldowns`

- `id` TEXT PRIMARY KEY
- `scope_type` TEXT NOT NULL
- `scope_id` TEXT NOT NULL
- `reason` TEXT NOT NULL
- `until_at` TEXT NOT NULL
- `created_at` TEXT NOT NULL

### `control_budgets`

- `id` TEXT PRIMARY KEY
- `budget_scope` TEXT NOT NULL
- `budget_key` TEXT NOT NULL
- `window_start` TEXT NOT NULL
- `window_end` TEXT NOT NULL
- `usage_count` INTEGER NOT NULL
- `usage_cost_usd` REAL NOT NULL DEFAULT 0
- `updated_at` TEXT NOT NULL

### `control_worker_runs`

- `id` TEXT PRIMARY KEY
- `task_id` TEXT
- `objective_id` TEXT
- `worker_kind` TEXT NOT NULL
- `runtime_name` TEXT NOT NULL
- `model_name` TEXT
- `attempt` INTEGER NOT NULL
- `status` TEXT NOT NULL
- `classification` TEXT
- `started_at` TEXT NOT NULL
- `ended_at` TEXT
- `breadcrumb_path` TEXT

### `control_breadcrumb_index`

- `id` TEXT PRIMARY KEY
- `entity_type` TEXT NOT NULL
- `entity_id` TEXT NOT NULL
- `worker_run_id` TEXT
- `classification` TEXT
- `path` TEXT NOT NULL
- `created_at` TEXT NOT NULL

### `control_recovery_actions`

- `id` TEXT PRIMARY KEY
- `action_type` TEXT NOT NULL
- `target_type` TEXT NOT NULL
- `target_id` TEXT NOT NULL
- `reason` TEXT NOT NULL
- `result` TEXT NOT NULL
- `created_at` TEXT NOT NULL

## Event Contract

V1 event types only:

- `api_down`
- `api_up`
- `harness_down`
- `harness_up`
- `task_ready`
- `task_completed`
- `task_failed`
- `task_timed_out`
- `queue_empty`
- `objective_stalled`
- `provider_degraded`
- `lane_paused`
- `lane_resumed`
- `merge_failed`
- `merge_succeeded`
- `post_merge_failed`

Each event persists:

- `event_type`
- `entity_type`
- `entity_id`
- `timestamp`
- `producer`
- `payload_json`
- `idempotency_key`

### Example Payloads

#### `api_down`

```json
{
  "endpoint": "/api/version",
  "status_code": null,
  "error": "connection refused"
}
```

#### `task_timed_out`

```json
{
  "task_id": "task_123",
  "worker_kind": "coding_worker",
  "runtime_name": "claude_code",
  "timeout_seconds": 1800,
  "log_path": "/path/to/log"
}
```

#### `objective_stalled`

```json
{
  "objective_id": "objective_123",
  "stall_class": "promotion_stalled",
  "hours_without_progress": 8,
  "failed_promotion_cycles": 2
}
```

## Health Checks

Use deterministic checks only.

Cron-like timers:

- `api-watch`: every 1 minute
- `harness-watch`: every 1 minute
- `loop-watch`: every 5 minutes

No planner cron in v1.

## Telegram Command Contract

Supported commands:

- `on`
- `off`
- `freeze`
- `thaw`
- `status`
- `pause worker`
- `resume worker`

### Semantics

- `on`: set master switch on, transition `OFF -> STARTING`
- `off`: stop dispatch, pause lanes, preserve state
- `freeze`: emergency halt, transition to `FROZEN`
- `thaw`: leave `FROZEN`, return to `STARTING`
- `pause worker`: pause coding lane
- `resume worker`: resume coding lane if system is not frozen

### `status` Response Shape

The status payload should be deterministic:

```json
{
  "global_state": "HEALTHY",
  "master_switch": true,
  "lanes": {
    "api": "RUNNING",
    "harness": "RUNNING",
    "worker": "RUNNING",
    "watch": "RUNNING",
    "telegram": "RUNNING"
  },
  "active_task_id": "task_123",
  "latest_failure_class": null,
  "cooldowns": [],
  "last_merge_status": "success",
  "frozen_reason": null
}
```

Telegram may render this in prose, but the underlying shape should not vary.

Operationally, `status` should be deterministic in v1. Prefer templates over models.

## Breadcrumb Contract

Breadcrumb is not memory. It is a compact handoff and audit layer.

Each bundle contains:

- `meta.json`
- `evidence.json`
- `decision.json`

Optional:

- `summary.txt`

### `meta.json`

```json
{
  "entity_type": "task",
  "entity_id": "task_123",
  "worker_run_id": "run_abc",
  "worker_kind": "coding_worker",
  "runtime_name": "claude_code",
  "model_name": "claude_code",
  "repo_sha": "abc123",
  "created_at": "..."
}
```

### `evidence.json`

```json
{
  "checks": [
    {"name": "api_version", "result": "pass"},
    {"name": "tests", "result": "fail"}
  ],
  "artifacts": [
    {"type": "command_output", "path": "..."},
    {"type": "log_excerpt", "path": "..."}
  ],
  "provider_error": null
}
```

### `decision.json`

```json
{
  "classification": "provider_rate_limit",
  "confidence": 0.94,
  "retry_recommended": false,
  "cooldown_seconds": 1800,
  "action_taken": "lane_paused",
  "uncertainty": "low"
}
```

### Do Not Store

- chain-of-thought
- long narrative memory
- duplicated raw logs inline
- transcript replay unless directly needed as evidence

Store bundles under:

- `.accruvia-harness/control/breadcrumbs/<entity>/<timestamp>-<agent>/`

Store in SQLite only:

- artifact id
- linked entity
- classification
- path
- timestamp

## Worker Input Packet

Every worker receives one bounded packet:

```json
{
  "task_id": "task_123",
  "objective_id": "objective_123",
  "repo_sha": "abc123",
  "scope_constraints": {
    "allowed_paths": ["src/", "tests/"],
    "forbidden_paths": [".github/"]
  },
  "required_checks": ["tests", "lint"],
  "prior_classification": null,
  "promotion_target": "main"
}
```

Workers should not rely on inherited chat transcripts.

## Coding Worker Contract

`coding-worker` uses:

- default: `Claude Code`
- fallback: `Opus 4.6`

### It May

- edit code within allowed scope
- run required checks
- produce artifacts

### It Must

- leave a breadcrumb bundle
- produce structured pass/fail evidence
- stop when required checks fail beyond policy
- respect merge gates

### It May Not

- retry on its own
- pick its own fallback model
- continue after `unknown` classification
- merge directly by itself

## Failure Classifier Contract

Default:

- `Gemini 2.5 Flash`
- fallback: `Gemini 3.1 Pro`

The output must be strict:

```json
{
  "class": "provider_rate_limit",
  "confidence": 0.94,
  "retry_recommended": false,
  "cooldown_seconds": 1800,
  "evidence": [
    "stderr: API rate limit reached",
    "log: provider returned 429"
  ]
}
```

Allowed classes:

- `timeout`
- `provider_rate_limit`
- `provider_outage`
- `credit_exhaustion`
- `hung_process`
- `system_failure`
- `merge_gate_failure`
- `unknown`

## Retry And Fallback Matrix

### Coding Worker

- first attempt: `Claude Code`
- on failure: classify first
- `hung_process` -> restart worker once
- `timeout` -> retry once if budget allows
- `provider_rate_limit` -> no retry, enter cooldown
- `provider_outage` -> no retry, enter cooldown
- `credit_exhaustion` -> pause worker lane
- `unknown` -> no fallback, escalate
- fallback to `Opus 4.6` only if classifier says retryable

### SA Watch

- first attempt: `Codex`
- one fallback to `Gemini 3.1 Pro`
- second failure freezes affected lane

### Minimal Hard-Coded Cooldown Defaults For V1

- `provider_rate_limit` -> 30 minutes
- `provider_outage` -> 30 minutes
- `credit_exhaustion` -> pause lane until operator action
- `unknown` -> no cooldown retry; escalate immediately after second occurrence
- `hung_process` -> one restart attempt, then pause lane

## Budget Policy

Define expensive classes now:

- `Claude Code` = expensive
- `Opus 4.6` = very expensive
- `Codex` = medium
- `Gemini 3.1 Pro` = medium
- `Gemini 2.5 Flash` = cheap

V1 defaults:

- max 3 expensive coding runs per hour
- max 1 fallback invocation per task
- 30-minute cooldown on provider degradation
- no fallback after `unknown`

These values should be implemented as constants first, not as a configurable subsystem.

## Objective Stalled Definitions

### Task Stalled

- runnable with no state change for 2 hours
- or same failure class 3 times

### Objective Stalled

- no task or objective state change for 6 hours
- or 3 repair loops without promotion progress

### Promotion Stalled

- 2 failed promotion cycles
- or merge gate failure repeats twice without net improvement

These emit `objective_stalled`.

## No-Progress Protection

Bad loops are not only failures.

Define `no_progress` as:

- 3 completed coding runs without advancing objective state toward merge

If triggered:

- pause worker lane
- write breadcrumb
- escalate to `sa-watch`

## Direct-To-Main Merge Gates

Before merge:

- repo clean-state check
- base branch fast-forward check
- required tests pass
- compile and lint checks pass
- diff scope allowed
- required artifacts present
- report valid
- system not frozen

If any fail:

- no merge
- write breadcrumb
- classify failure
- route to recovery or escalate

### Merge Ownership

The final merge action should be executed by a deterministic control-plane path after all gates pass. The coding worker must never be the authority that decides “merge now.”

## Post-Merge Rollback Policy

After merge, run post-merge smoke validation.

### Post-Merge Failure Means

- deterministic smoke validation fails once
- or API or harness health breaks immediately after merge in a way attributable to the merged change

If post-merge failure occurs:

1. revert the last merge commit automatically
2. emit `post_merge_failed`
3. freeze the system
4. write critical breadcrumb
5. create corrective task or objective in the harness
6. escalate to operator

### Post-Merge Validation Window

For v1, post-merge validation should be narrow and deterministic:

- required smoke command completes within a fixed timeout
- API liveness remains healthy after merge
- harness liveness remains healthy after merge

Do not add broad flaky integration checks to the rollback trigger in v1.

## Ordinary Recovery Logic

The control plane should do these without an LLM:

- if API is down: restart once, recheck
- if harness is down: restart once, recheck
- if worker process is hung: kill once, requeue classification
- if provider degraded: enter cooldown
- if same failure repeats: pause lane

Only escalate to `sa-watch` when deterministic recovery fails or the issue looks structural.

## Human Escalation Rules

Escalate when:

- post-merge rollback occurred
- `unknown` classification happens twice
- system remains `DEGRADED` for more than 30 minutes
- merge blocked twice
- budget exhausted with active critical work
- objective stalled after one recovery cycle

Escalation payload should contain:

- known facts
- failed checks
- paused or frozen lanes
- recommended next operator action

## SA Watch Contract

`sa-watch` exists only for catastrophic intervention.

It may:

- kill
- pause
- restart
- freeze a lane
- suppress retries
- escalate

It should not create normal improvement work in v1.

It should prefer, in order:

1. pause or freeze
2. kill and restart once
3. suppress repeated retries
4. escalate

Only after failed recovery should it create corrective work.

## Success Metrics

Track these from day one:

- API uptime
- harness uptime
- merge success rate
- post-merge rollback count
- retries per task
- fallback rate
- provider degradation incidents
- stalled objective count
- cost per merged objective
- no-progress freezes

## V1 Build Order

1. add DB schema
2. add global and lane state transitions
3. add event store
4. add breadcrumb writer and indexer
5. add deterministic health checks
6. add Telegram command and status layer
7. add failure classifier
8. add coding worker integration
9. add direct-to-main merge gates
10. add rollback logic
11. add `sa-watch`

### Phase Checklists

Each phase is only complete when it leaves behind:

- passing tests for the new deterministic behavior
- one example breadcrumb bundle
- one documented failure case
- one operator-visible status output demonstrating the new capability

### Named Acceptance Tests

V1 should include at least these tests:

- `test_api_down_restarts_once_and_recovers`
- `test_harness_down_restarts_once_and_recovers`
- `test_hung_worker_classifies_and_restarts_once`
- `test_provider_rate_limit_enters_cooldown`
- `test_merge_gate_failure_blocks_merge`
- `test_post_merge_failure_triggers_revert_and_freeze`
- `test_no_progress_freezes_worker_lane`
- `test_status_output_is_deterministic_without_model`

## Breadcrumb Retention

Use a simple retention policy in v1:

- keep full breadcrumb bundles for 14 days
- keep indexed metadata in SQLite longer
- allow explicit archival later

Do not build a complex cleanup subsystem in v1.

## V1 Success Criteria

V1 succeeds when:

- API and harness recover from ordinary failures deterministically
- coding work proceeds without chat-memory dependence
- failures are classified durably
- expensive fallbacks are bounded
- merges to `main` are safe enough to trust
- rollback works when needed
- `sa-watch` can stop runaway loops
- Telegram acts as control and reporting only
