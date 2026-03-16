# Product Architecture

## Purpose

This document defines the top-level product architecture for `accruvia-harness` as it evolves from a local developer tool into an objective-centered web application.

It is the architectural reference point for:

- backend technology choices
- frontend product shape
- domain model boundaries
- local-first deployment
- future hosted multi-tenant deployment

It exists to settle the architecture before more top-of-funnel UX or workflow implementation is added.

## Product Thesis

The harness is not primarily a code-generation shell around an LLM.

It is an objective planning and execution control plane.

The product should therefore be organized around:

1. objective intake
2. architecture and workflow modeling
3. comprehensive planning
4. atomic slice execution
5. evidence and resolution

## Top-Level Product Flow

The intended product spine is:

1. `Objective Intake`
   - collect the initial broad goal
   - structure knowns, assumptions, and open questions

2. `Architecture / Workflow Modeling`
   - generate a high-level system or workflow representation
   - use diagrams and dependency maps to surface missing integrations and control logic

3. `Comprehensive Objective Planning`
   - generate the broad objective-level plan
   - not directly executable

4. `Atomic Slice Selection And Refinement`
   - choose one bounded executable slice
   - validate/refine it deterministically

5. `Task Execution`
   - run one approved atomic slice

6. `Evidence And Objective Resolution`
   - analyze results
   - decide what remains unresolved
   - choose the next slice or conclude the objective

## Primary Domain Objects

The product should elevate these objects to first-class status:

- `Organization`
- `Workspace`
- `Objective`
- `IntakePacket`
- `ArchitectureDiagram`
- `ObjectivePlan`
- `AtomicSlice`
- `Task`
- `Run`
- `Artifact`
- `Evaluation`
- `Decision`

Important implication:

- `Task` is not the top-level product object anymore
- `Objective` is the top-level product object

## Domain Hierarchy

The core lineage should be:

- `Objective`
  - broad business or engineering goal

- `ObjectivePlan`
  - comprehensive non-executable plan for that objective

- `AtomicSlice`
  - machine-checkable execution slice selected from the objective plan

- `Task`
  - executable unit derived from one approved atomic slice

- `Run`
  - one concrete attempt to implement that task

This hierarchy should be visible in both the API and the UI.

## Architecture Pattern

The recommended pattern is:

- `local-first control plane`
- `hosted-ready boundaries`

This means:

- local mode is the default runtime experience
- the control-plane boundaries should already match a hosted product
- “runs on a dev’s machine” is a deployment mode, not the product definition

## Backend Choice

Recommended backend:

- `FastAPI`

Why:

- API-first product
- strong typed request/response contracts
- good fit for a web UI and local+hosted reuse
- better aligned with orchestration/state APIs than a server-rendered admin stack

Why not use `Django` as the primary backend:

- the core problem is not generic business CRUD
- the product needs specialized planning/execution APIs
- Django admin is not the long-term UI for objectives, plans, and control logic

The one thing Django would help with, internal inspection/admin, can be addressed in the main web UI or a lightweight internal admin surface later.

## Frontend Choice

Recommended frontend:

- `Vue`
- `Vuetify`

Why:

- strong fit for operator dashboards and structured workflow tooling
- easier to impose standardized business-app interaction patterns
- good for objective-centered forms, evidence views, and graph/diagram surfaces

The product should be a web app first, even if the backend continues to run locally in v1.

## Backend Structure

The open-source library/repo should be structured into at least these layers:

### 1. Core Domain Library

Contains:

- domain objects
- planning policies
- atomicity rules
- run/evaluation/decision logic
- telemetry extraction

This layer should not depend on FastAPI or UI concerns.

### 2. Application Services

Contains:

- objective orchestration services
- plan refinement services
- task/run services
- diagram services
- query/interrogation services

This layer coordinates the domain model for API use.

### 3. API Server

FastAPI layer exposing:

- REST endpoints
- WebSocket or streaming endpoints later if needed
- auth boundary later for hosted mode
- local session behavior for v1

### 4. Runtime Adapters

Contains:

- worker execution adapters
- CLI/MCP/tool integrations
- local runtime contracts
- future hosted executor contracts

### 5. Persistence Layer

Contains:

- local SQLite storage now
- Postgres-compatible schema and repositories later
- lineage and event storage

## Frontend Product Areas

The UI should be built around objectives and evidence, not a flat task table.

Suggested primary surfaces:

### 1. Objective View

Shows:

- broad objective
- intake packet
- current status
- importance
- difficulty
- completion judgment
- unresolved areas

### 2. Diagram View

Shows:

- high-level architecture/workflow Mermaid
- red-team passes
- diagram revisions
- control-flow loops when they appear

### 3. Planning View

Shows:

- comprehensive objective plan
- atomic slices
- slice revisions
- validator outputs

### 4. Execution View

Shows:

- tasks
- runs
- artifacts
- validations
- failures
- retries and decomposition

### 5. Evidence View

Shows:

- diffs
- tests
- reports
- atomicity telemetry
- plan alignment
- decision rationale

## API Surface

The API should be designed around the domain hierarchy, not around legacy task-only workflows.

Minimum resource families:

- `/objectives`
- `/objectives/{id}/intake`
- `/objectives/{id}/diagrams`
- `/objectives/{id}/plans`
- `/plans/{id}/slices`
- `/tasks`
- `/runs`
- `/artifacts`
- `/evaluations`
- `/decisions`
- `/query`

Important rule:

- tasks and runs must remain queryable in the context of objectives and plans

## Local Mode

Local mode should be the default developer experience.

Characteristics:

- backend runs on the developer machine
- frontend talks to local API server
- local worker/tool execution
- SQLite by default
- single local organization/workspace by default

The local app should still expose the same domain model as the hosted version.

## Hosted Mode

Hosted mode should be a deployment evolution, not a product rewrite.

Expected changes later:

- managed auth
- multi-tenant `Organization` / `Workspace` enforcement
- Postgres instead of SQLite
- object storage for artifacts
- remote or pooled executors
- separate worker placement

The control-plane model should stay the same.

## Multi-Tenant Readiness

Even in local mode, the schema and API should reserve first-class concepts for:

- `Organization`
- `Workspace`

In local mode they can default to:

- one org
- one workspace

But they should exist from the start so hosted mode does not require a conceptual rewrite.

## Planning And Execution Separation

The architecture should preserve two distinct funnels:

### Planning Funnel

- objective intake
- architecture/workflow diagram
- red-team passes
- comprehensive objective plan
- candidate atomic slice
- deterministic atomic validation
- slice refinement

### Execution Funnel

- task creation
- run execution
- attempt telemetry
- attempt-time atomicity gate
- deterministic validation
- run analysis
- run decisioning
- objective resolution

This separation is essential.

It is one of the main lessons from the current harness.

## Diagram Strategy

Diagrams should be first-class artifacts in the product.

There are at least two diagram types:

1. `Architecture / workflow diagrams`
   - early high-level system structure
   - external APIs and dependencies
   - business/process modeling

2. `Control-flow diagrams`
   - plans, tasks, runs, retries, splits, recursion
   - useful when loops or decomposition become hard to reason about

These should be viewable and revisable in the product UI.

## Standardized Build Patterns

The product should support constrained generation patterns rather than arbitrary tech stacks.

Examples:

- Python backends
- Vue/Vuetify frontends
- standard deployment scaffolds
- standard business-ops integrations

This will make generated systems:

- easier to operate
- easier to validate
- easier to host later
- easier for business-oriented users to understand

## Non-Goals

This architecture does not assume immediate:

- multi-tenant SaaS launch
- heavy auth stack
- polished business-user drag-and-drop builder
- distributed microservices everywhere

It does assume:

- objective-centered product design
- API-first backend
- web-first UI
- local-first runtime with hosted-ready boundaries

## Immediate Architectural Commitments

The architecture should be treated as settled around these commitments:

1. `FastAPI` backend
2. `Vue + Vuetify` frontend
3. `Objective` as the top-level product object
4. `ObjectivePlan` and `AtomicSlice` as separate planning layers
5. diagrams as first-class artifacts
6. strict atomicity by default for executable slices
7. local-first deployment with hosted-ready boundaries
