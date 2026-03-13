# Product Plan

This document is the comprehensive plan for building `accruvia-harness` into a durable LLM-enabled workflow system for creating and managing LLM-developed software.

## Product Goal

Build a harness that can:

- accept work from external systems such as GitLab issues
- convert that work into internal executable tasks
- run a durable loop of `plan -> work -> analyze -> decide -> repeat`
- manage retries, promotion, follow-on work, and reporting explicitly
- produce an auditable record of what happened and why
- support multiple projects and, later, safe parallel execution

## Core Product Principles

- Internal execution truth is canonical.
- External systems are inputs and reporting surfaces, not the control plane.
- Artifacts, evaluations, decisions, and events are first-class records.
- Workflow policy must be encoded explicitly, not inferred from chat transcripts.
- Every run must end with usable evidence: promotion-ready output, precise failure, or blocked diagnosis.
- The system must be able to explain why it took an action.

## Scope Boundaries

### In Scope

- software-development task orchestration
- durable state and event history
- issue import and result reporting
- worker execution abstractions
- evaluation and promotion policy
- queueing, prioritization, and bounded retries
- productivity and throughput analysis

### Out Of Scope For V1

- payments
- user authentication
- hosted multi-tenant product features
- generalized non-software workflow automation
- fully autonomous product strategy without human oversight

## Product Architecture

### Core Subsystems

1. Control plane
   - canonical records for projects, tasks, runs, artifacts, evaluations, decisions, and events
   - migration-managed database
   - policy history and replayability

2. Workflow engine
   - durable long-running workflow execution
   - timers, retries, backoff, lease management
   - queue selection and parallel coordination
   - evolve toward `Split-Phase Execution` so candidate generation, deterministic validation, and decisioning can be scheduled independently

3. Worker runtime
   - adapters for code-generation workers
   - bounded execution environments
   - artifact capture and result normalization

4. Evaluation and promotion
   - happy-path verification
   - completeness checks
   - repo-specific validation
   - promotion and rejection rules

5. External integrations
   - GitLab issue import
   - GitLab reporting and closure
   - future integrations such as other trackers or code hosts

6. Interrogation and analytics
   - operational summaries
   - productivity metrics
   - throughput, latency, retry, and failure-pattern analysis
   - optional OpenClaw observer surface

## Build Phases

### Phase 0: Foundation

Goal:
- prove the domain model and control loop locally

Deliverables:
- local package and CLI
- internal task/run/artifact/evaluation/decision/event records
- local happy-path execution
- local tests for core behavior

Exit criteria:
- a task can be created, executed, evaluated, and finalized locally
- the system can explain what happened from stored records

### Phase 1: Durable Local Harness

Goal:
- make the current local harness safe and extensible

Deliverables:
- migration system for schema changes
- explicit configuration model
- structured logging and error taxonomy
- richer event payloads
- local smoke-test command
- repository hygiene around generated artifacts and state

Exit criteria:
- schema evolution is no longer ad hoc
- operators can diagnose failures from logs and events
- local setup and testing are reliable and documented

### Phase 2: Real Workflow Runtime

Goal:
- replace ad hoc synchronous execution with a durable workflow engine

Deliverables:
- adopt `Temporal` as the workflow runtime
- map `plan -> work -> analyze -> decide -> repeat` onto workflow/state-machine steps
- durable retry and timer semantics
- activity abstractions for worker execution and evaluation

Exit criteria:
- workflows survive process restarts
- retries and timing behavior are engine-controlled
- control flow is no longer tied to one synchronous CLI process
- deterministic validation no longer monopolizes editing throughput

Reference:
- `specs/split-phase-execution.md`

### Phase 3: Real Worker Abstractions

Goal:
- support multiple worker implementations cleanly

Deliverables:
- worker interface and result contract
- support for local workers and agent-backed workers
- normalized artifact capture
- bounded execution policies

Exit criteria:
- workers can be swapped without changing task/evaluation storage
- every run leaves promotion-ready output, precise failure, or blocked diagnosis

### Phase 4: Evaluation And Promotion

Goal:
- make the system trustworthy about quality

Deliverables:
- repo-specific validation adapters
- candidate completeness checks
- promotion/rejection policy
- follow-on task generation for discovered defects
- bounded requeue logic

Exit criteria:
- successful runs are not promoted unless artifacts are complete
- failed promotion produces actionable feedback
- follow-on work is recorded with lineage

### Phase 5: GitLab Workflow Integration

Goal:
- make GitLab a strong intake and reporting surface

Deliverables:
- issue import and sync policies
- deduplication and idempotency guarantees
- status reporting back to issues
- structured completion/failure comments
- optional close/reopen policy

Exit criteria:
- open issues can be synchronized into internal tasks safely
- task outcomes can be reported back without losing harness control

### Phase 6: Parallel Execution

Goal:
- support safe concurrency

Deliverables:
- queue arbitration
- worker lease model
- concurrency limits per project and per task class
- speculative branch support
- winner selection and branch disposal policy

Exit criteria:
- multiple tasks can run at once safely
- multiple branches of one task can be compared without state corruption

### Phase 7: Observability And Analytics

Goal:
- make the harness inspectable as an operating system

Deliverables:
- `OpenTelemetry`
- structured metrics and traces
- run dashboards
- throughput and retry analytics
- cost and latency metrics

Exit criteria:
- operators can answer why the system is slow, stuck, or wasteful
- productivity can be measured over time

### Phase 8: Interrogation Layer

Goal:
- support LLM-assisted analysis of harness behavior without giving up control

Deliverables:
- read-only query surfaces over harness state
- summarized context packets for strategic analysis
- OpenClaw or equivalent observer integration

Exit criteria:
- an LLM can explain system behavior from structured evidence
- the observer layer cannot silently mutate execution truth

## Cross-Cutting Requirements

### Data & Schema

- migration-managed schema changes
- durable identifiers and lineage
- append-only event history
- explicit external-reference model
- artifact metadata and provenance

### Testing

- unit tests for domain and policy
- integration tests for storage and CLI
- mocked external-integration tests
- workflow replay tests
- regression tests for retry, promotion, and follow-on-task behavior

### Reliability

- idempotent imports
- bounded retries
- deterministic failure classification
- resumable workflows
- clear blocked-state handling

### Documentation

- architecture doc
- operator runbook
- workflow policy doc
- external integration policy
- migration and versioning policy

## Immediate Next Milestones

1. Add schema migrations and configuration management.
2. Split workflow policy concerns out of the monolithic engine.
3. Add a local smoke-test command for import -> process -> report.
4. Introduce structured logging and a clearer error taxonomy.
5. Move from SQLite proof-of-concept toward the intended runtime stack.

## Current Known Constraints

- the current implementation is still local and synchronous
- `Temporal`, `LangGraph`, `PostgreSQL`, `MLflow`, and `OpenTelemetry` are planned but not yet integrated
- there is no deployment or staging story yet
- GitLab integration currently depends on `glab`

## Product Success Criteria

- the harness can run durable software-development loops without prompt-driven orchestration
- multiple projects can coexist without state confusion
- external issues can be synchronized without becoming the control plane
- every run yields auditable evidence
- retries and promotions are explicit, bounded, and explainable
- productivity and throughput can be measured directly from stored records
