# Operator Runbook

## Purpose

This runbook covers the local operation of `accruvia-harness` during the current prototype stage.

## Canonical Truth

- external issue systems are intake and reporting surfaces
- the harness database and event history are canonical execution truth
- do not treat issue comments or chat transcripts as authoritative workflow state

## Local Startup

```bash
PYTHONPATH=src python3 -m accruvia_harness config
PYTHONPATH=src python3 -m accruvia_harness init-db
PYTHONPATH=src python3 -m accruvia_harness smoke-test
```

## Core Operator Commands

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m accruvia_harness status
PYTHONPATH=src python3 -m accruvia_harness summary
PYTHONPATH=src python3 -m accruvia_harness context-packet
PYTHONPATH=src python3 -m accruvia_harness task-report <task_id>
PYTHONPATH=src python3 -m accruvia_harness dashboard-report
PYTHONPATH=src python3 -m accruvia_harness telemetry-report
PYTHONPATH=src python3 -m accruvia_harness explain-system
PYTHONPATH=src python3 -m accruvia_harness explain-task <task_id>
PYTHONPATH=src python3 -m accruvia_harness events
```

## Read-Only Observer Boundary

- `context-packet`, `task-report`, `dashboard-report`, `explain-system`, and `explain-task` are observer commands
- they operate through a read-only facade over the store
- they must not mutate tasks, runs, promotions, or events

## Issue Intake And Reporting

```bash
PYTHONPATH=src python3 -m accruvia_harness sync-github-open <project_id> <repo>
PYTHONPATH=src python3 -m accruvia_harness report-github <task_id> <repo>
PYTHONPATH=src python3 -m accruvia_harness sync-github-state <task_id> <repo>
PYTHONPATH=src python3 -m accruvia_harness sync-github-metadata <task_id> <repo>
PYTHONPATH=src python3 -m accruvia_harness sync-gitlab-open <project_id> <repo>
PYTHONPATH=src python3 -m accruvia_harness process-next --worker-id worker-a --lease-seconds 300
PYTHONPATH=src python3 -m accruvia_harness report-gitlab <task_id> <repo>
PYTHONPATH=src python3 -m accruvia_harness sync-gitlab-state <task_id> <repo>
PYTHONPATH=src python3 -m accruvia_harness sync-gitlab-metadata <task_id> <repo>
```

## Queue Arbitration

- use explicit `worker_id` values when more than one operator or process may process tasks
- lease state is internal truth for queue ownership
- expired leases are cleared automatically when the next worker asks for work

To inspect current leases:

```bash
PYTHONPATH=src python3 -m accruvia_harness status
```

## Workspace Safety Policy

Workspace isolation is now an explicit project policy.

- `isolated_required`: refuse adapters that point at a shared repo checkout
- `isolated_preferred`: allow execution but do not require isolation
- `shared_allowed`: permit shared-repo execution

For autonomous code work, the default should stay `isolated_required`.

Reason:

- blocked or failed runs must not pollute the main repo
- parallel tasks need isolated filesystem state
- promotion should happen from isolated results back into the real branch, not by mutating the live checkout in place

## Promotion Delivery Policy

Approved isolated work can be delivered in one of three modes:

- `direct_main`
- `branch_only`
- `branch_and_pr`

Recommended default:

- `branch_and_pr`

Use `direct_main` only after a project has proven it can operate safely without routine manual rescue.

## Generated State

Generated state lives under `.accruvia-harness/` and should not be committed:

- database
- logs
- telemetry journal and replay state
- workspace artifacts

If local state becomes confusing during development:

```bash
rm -rf .accruvia-harness
PYTHONPATH=src python3 -m accruvia_harness init-db
```

## Telemetry Durability

Telemetry is journal-first:

- `.accruvia-harness/telemetry/journal.jsonl` is the durable append log
- metrics, spans, and warnings are materialized views
- replay state lives in `.accruvia-harness/telemetry/telemetry_state.json`

If the process crashes mid-run, the next telemetry read or process startup replays unapplied journal entries.

To inspect telemetry health:

```bash
PYTHONPATH=src python3 -m accruvia_harness telemetry-report
```

Watch for:

- `journal_backlog`
- `otel_warning`
- repeated export warnings in `warnings`

## Localized Trial Checklist

Review these before using the harness against a private workload:

1. Exact private repo adapter behavior
2. Worker command and environment exposure for that repo
3. Promotion rules for that workload
4. Timeout and resource defaults for realistic tasks
5. Observer channels and notification noise
6. Backup and retention for `.accruvia-harness/`
7. Trial runbook steps below

## Trial Runbook

### Start

```bash
make init
make test-fast
make test-temporal
PYTHONPATH=src python3 -m accruvia_harness init-db
PYTHONPATH=src python3 -m accruvia_harness config
```

### Stop

- stop any local worker process cleanly
- stop any Temporal worker cleanly
- ensure no unexpected active leases remain in `status`

### Recover stale state

Stale state is recovered automatically on startup by `init-db` and any normal store initialization path.

To force a clean process restart:

```bash
PYTHONPATH=src python3 -m accruvia_harness init-db
PYTHONPATH=src python3 -m accruvia_harness status
```

### Inspect blocked tasks

```bash
PYTHONPATH=src python3 -m accruvia_harness summary
PYTHONPATH=src python3 -m accruvia_harness dashboard-report
PYTHONPATH=src python3 -m accruvia_harness task-report <task_id>
PYTHONPATH=src python3 -m accruvia_harness events --entity-type task --entity-id <task_id>
```

### Rerun safely

- verify the task is `pending` or explicitly requeued
- review the latest run, evaluation, decision, and promotion records first
- if Temporal/runtime behavior changed, rerun `make test-temporal` before retrying real work

Preferred rerun path:

```bash
PYTHONPATH=src python3 -m accruvia_harness run-until-stable <task_id>
```

Or queue-driven:

```bash
PYTHONPATH=src python3 -m accruvia_harness process-next --worker-id worker-a --lease-seconds 300
```

## Backup And Retention

Before a localized trial, back up `.accruvia-harness/` periodically, especially:

- `harness.db`
- `telemetry/journal.jsonl`
- `workspace/` if you need retained artifacts

At minimum:

- snapshot before schema/runtime changes
- snapshot before trial start
- snapshot after any blocked or failed production-like incident

## What To Check When Something Breaks

1. Run the test suite.
2. Inspect `.accruvia-harness/harness.log`.
3. Inspect task and run events with `events`.
4. Confirm schema version with `init-db` or `status`.
5. If GitHub integration is involved, confirm `gh auth status`.
6. If GitLab integration is involved, confirm `glab` authentication separately.
7. If OTLP export is expected, confirm `ACCRUVIA_OTEL_EXPORTER_OTLP_ENDPOINT` is set and reachable.
