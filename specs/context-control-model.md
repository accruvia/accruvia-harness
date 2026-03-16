# Context Control Model

## Purpose

This document defines the control model for the harness as it shifts from task-centric automation to objective-centered software delivery.

It exists to make these points explicit:

- context management is mandatory
- intent must be modeled directly, not inferred only from code or chat
- Mermaid diagrams are execution-governing process artifacts
- frustration is a first-class divergence signal
- execution is blocked when required control artifacts are unclear

## Core Thesis

Machine coding is no longer blocked primarily by code generation.

It is blocked by:

- unclear operator intent
- stale or incorrect plans
- process/control-flow ambiguity
- drift between plan, code, runtime behavior, and operator experience

The harness therefore needs a mandatory context manager and a mandatory process-control surface.

## Source Of Truth

Local mode uses a two-tier model:

1. harness database and artifact files
   - canonical source of operational truth
   - projects, objectives, plans, tasks, runs, artifacts, events, context records

2. context manager layer
   - required function
   - local-first by default
   - may later sync to or adopt an approved external Open Brain backend

Important distinction:

- context management is mandatory
- a specific external service is not mandatory

## Top-Level Hierarchy

The control-plane hierarchy is:

- `Project`
  - codebase and repository context

- `Objective`
  - one meaningful desired outcome or problem within the project

- `IntentModel`
  - explicit model of what the operator actually wants

- `MermaidArtifact`
  - process/control/state representation that governs downstream execution

- `Plan`
  - implementation-oriented plan derived from the current intent and process model

- `AtomicSlice`
  - bounded executable unit

- `Task`
  - execution wrapper for one approved slice

- `Run`
  - one concrete attempt

## Required Execution Gates

Code execution may proceed only when all required gates are satisfied:

- current objective exists
- current intent model exists
- required Mermaid artifact exists and is `finished`
- current plan exists
- current approved atomic slice exists

If a required gate is missing or stale, the harness must not proceed to execution. It must route the operator to the missing control surface.

## Intent Model

The intent model captures what the operator wants independently from the implementation plan.

Minimum fields:

- `intent_summary`
- `success_definition`
- `non_negotiables`
- `preferred_tradeoffs`
- `unacceptable_outcomes`
- `known_unknowns`
- `operator_examples`
- `frustration_signals`
- `sop_constraints`
- `current_confidence`
- `last_confirmed_at`

Rules:

- the system may propose revisions
- the operator is final authority
- revisions are versioned
- intent must never be silently overwritten

## Mermaid As Process Control

If there is a process, there must be a Mermaid artifact.

Mermaid is required when any of these are present:

- process or workflow behavior
- multiple sequential steps
- branching logic
- loops or retries
- status or lifecycle transitions
- multiple actors or roles
- human approval points
- external systems or APIs
- data handoffs
- async or scheduled behavior
- failure handling or fallback behavior
- frustration or confusion
- investigation mode
- repeated non-convergence
- operator request

Standard Mermaid types:

- `workflow/control-flow`
- `system/integration architecture`
- `investigation divergence map`
- `state/lifecycle diagram`

### Mermaid Lifecycle

Mermaid artifacts are versioned and append-only.

Statuses:

- `draft`
- `in_review`
- `paused`
- `finished`
- `superseded`

Semantics:

- `paused`
  - work is unresolved
  - dependent execution stays blocked
  - evidence gathering and investigation may continue

- `finished`
  - operator accepts this as the best current process/control representation
  - dependent execution may proceed
  - future evidence may supersede it with a newer version

The operator is the final authority for marking a Mermaid artifact `finished`.

### Mermaid Discussion

Mermaid is built through CLI/UI interaction.

Rules:

- Mermaid discussion is part of the main conversation, not a separate chat system
- once Mermaid mode begins, downstream execution stays blocked until the diagram is:
  - `paused`
  - or `finished`
- “finished” creates a new accepted version
- the system must never silently mutate the current accepted version

## Investigation And Divergence

Investigation compares reality against the control ladder:

- intent -> Mermaid
- Mermaid -> plan
- plan -> code
- code -> runtime behavior
- runtime behavior -> telemetry expectation
- telemetry expectation -> operator experience

Primary divergence categories:

- `intent != plan`
- `plan != code`
- `code != runtime behavior`
- `runtime behavior != telemetry expectation`
- `telemetry != operator experience`
- `objective != current relevance`
- `intended solution != dependency reality`

Investigation outputs:

- likely root cause
- evidence summary
- divergence category
- recommended next action
- confidence

## Frustration

Frustration is a first-class signal.

It should be treated as evidence that intent and reality have diverged.

Rules:

- frustration is always recorded as a typed record
- frustration is inferred as an accumulating signal, not only via explicit statements
- frustration triggers deeper triage and likely-cause analysis
- the system should explicitly tell the operator:
  - it detects justified frustration
  - likely causes
  - recommended next action

Frustration scoring is primarily:

- per objective

And secondarily:

- per project
- per operator

## Context Records

The harness must capture both:

1. normalized first-class records
2. broad raw journal events

Normalized records include:

- objective
- operator_comment
- operator_frustration
- intent_model
- mermaid_snapshot
- plan
- atomic_slice
- task
- run
- artifact_summary
- validation_result
- investigation
- decision
- lesson

Raw journal capture should include almost everything relevant to:

- operator messages
- LLM messages
- prompts
- tool calls/results
- commands
- stdout/stderr
- telemetry transitions
- artifact metadata
- retrieval events

Operator input and LLM output must remain separate in the UI, but both belong to the same unified context system.

## Prompt Inclusion Strategy

Context storage is unified. Prompt inclusion is selective.

Use a layered prompt model:

1. live working set
2. rolling summaries
3. retrieval layer
4. raw journal

This keeps cost under control without splitting memory into isolated silos.

## Model Switching

Cheaper models should be used for:

- context classification
- summarization
- frustration triage
- retrieval ranking
- memory cleanup and compaction

Stronger models should be used for:

- planning
- investigation reasoning
- coding
- high-value decisioning

This is a strong `Routellect`-style use case for model switching by task type.

## Local-First Open Brain Compatibility

The harness should work out of the box locally without external services.

If an existing Open Brain instance is available:

- detect it
- explain what would be shared
- ask permission
- connect only if approved

External Open Brain, Supabase, or other backends are optional backends for the required context-manager function.
