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
PYTHONPATH=src python3 -m accruvia_harness events
```

## GitLab Intake And Reporting

```bash
PYTHONPATH=src python3 -m accruvia_harness sync-gitlab-open <project_id> <repo>
PYTHONPATH=src python3 -m accruvia_harness process-next --worker-id worker-a --lease-seconds 300
PYTHONPATH=src python3 -m accruvia_harness report-gitlab <task_id> <repo> --comment "..." --close
```

## Queue Arbitration

- use explicit `worker_id` values when more than one operator or process may process tasks
- lease state is internal truth for queue ownership
- expired leases are cleared automatically when the next worker asks for work

To inspect current leases:

```bash
PYTHONPATH=src python3 -m accruvia_harness status
```

## Generated State

Generated state lives under `.accruvia-harness/` and should not be committed:

- database
- logs
- workspace artifacts

If local state becomes confusing during development:

```bash
rm -rf .accruvia-harness
PYTHONPATH=src python3 -m accruvia_harness init-db
```

## What To Check When Something Breaks

1. Run the test suite.
2. Inspect `.accruvia-harness/harness.log`.
3. Inspect task and run events with `events`.
4. Confirm schema version with `init-db` or `status`.
5. If GitLab integration is involved, confirm `glab` authentication separately.
