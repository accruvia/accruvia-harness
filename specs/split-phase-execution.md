# Split-Phase Execution

## Purpose

`Split-Phase Execution` is the architectural direction for decoupling candidate generation from deterministic validation and post-validation decisioning.

The goal is to stop long-running validation from monopolizing the control loop. Editing throughput, deterministic verification, and strategy should be separate control-plane concerns with explicit state and bounded concurrency.

## Problem

The current synchronous run model does too much in one linear attempt:

1. plan
2. edit
3. deterministic validation
4. analyze
5. decide

That shape creates three failures:

- one task can monopolize the loop while validation runs
- the brain overfits to the latest blocked or timing-out run instead of reasoning over a project portfolio
- validation timeouts are a throughput bottleneck rather than just another observable result

## Principle

Long deterministic validation is not part of editing throughput.

Treat validation as its own queue.

## Target Lifecycle

Each attempt should be modeled as three explicit phases:

1. `candidate_generation`
   - produce code changes and durable artifacts in an isolated workspace
   - finish quickly
   - publish an immutable attempt snapshot

2. `deterministic_validation`
   - validate the immutable snapshot asynchronously
   - use a validation policy appropriate to task class
   - record startup timeout, execution timeout, early failures, and pass/fail evidence explicitly

3. `decisioning`
   - consume validation results
   - decide retry, decompose, fail, promote, or branch
   - create follow-on work if needed

## Snapshot Contract

Asynchronous validation is only safe if it runs against an immutable attempt snapshot.

Minimum snapshot requirements:

- frozen workspace path or archived diff/content bundle
- prompt, plan, and candidate summary artifacts
- explicit changed-file inventory
- explicit validation policy
- provenance linking snapshot to project, task, run, and attempt

Validation must never read from a mutable live workspace that an editor can still change.

## Queues

The long-term design should separate these queues logically, even if they still run in one process at first:

- `planning_queue`
- `editing_queue`
- `validation_queue`
- `decision_queue`

The system does not need separate services on day one, but it does need separate state and scheduling semantics.

## Concurrency Model

Concurrency should be controlled independently for editing and validation.

Why:

- editing workers consume the scarcest and most failure-prone resources
- validation workers can be scaled or capped differently
- strategy should continue while validation is in flight

## Failure Semantics

Split-phase execution should classify failures more precisely:

- `validation_startup_timeout`
- `validation_timeout`
- `validation_failed_fast`
- `executor_process_failure`
- `snapshot_creation_failure`
- `decisioning_failure`

These should not all collapse into one generic task failure.

## Brain Guidance

The brain should reason about in-flight work at portfolio level, not just the latest blocking run.

Implications:

- validation backlog is a first-class signal
- long validation is not a reason to stop proposing other bounded work
- if all attention collapses onto one issue for too long, task selection is probably scoped too narrowly
- task design should assume candidate generation and validation are decoupled

## Operator UX

The operator should be able to see:

- how many attempts are generating candidates
- how many attempts are validating
- validation queue age and bottlenecks
- whether the system is blocked on editing, validation, or decisioning
- whether healthy idle means “nothing queued” or “waiting on validation”

## Non-Goals

This design does not require immediate:

- distributed microservices
- remote message brokers
- full Temporal migration in one step

It does require explicit phase separation and immutable validation inputs.

## Incremental Rollout

Recommended sequence:

1. explicit validation policies per task class
2. split validation startup timeout from execution timeout
3. introduce attempt state that distinguishes editing from validation
4. create immutable per-attempt snapshots
5. add an asynchronous validation queue and worker
6. make decisioning consume validation results asynchronously
7. teach the brain and status surfaces to reason over in-flight validation backlog

## Success Criteria

The design is working when:

- long validation no longer stalls overall planning throughput
- operator and repair tasks use small, fast deterministic checks by default
- the harness can continue planning and editing bounded work while prior attempts validate
- validation failures are precise and actionable
- operator status clearly distinguishes editing, validating, and idle states
