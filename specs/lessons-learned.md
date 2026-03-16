# Lessons Learned

## Purpose

This document captures the highest-value product and architecture lessons learned while pushing `accruvia-harness` through self-hosting reliability work, atomicity failures, decomposition churn, and planning redesign.

It exists to preserve the important takeaways when conversational context is lost.

This is not a changelog.

It is the short list of things that proved true and should shape future product decisions.

## Core Lessons

### 1. Planning Is The Product

The biggest shift is that the harness is not primarily a code-generation wrapper.

It is a planning and control-plane product.

What determines outcome quality most strongly is:

- whether the objective is understood correctly
- whether the plan is coherent
- whether the next slice is chosen well
- whether the slice is atomic enough to validate cheaply and truthfully

The harness should therefore invest heavily in:

- objective intake
- comprehensive objective planning
- atomic slice selection
- plan refinement
- plan-to-run alignment

This means the harness should not behave like:

- “receive task, generate code, run tests, retry”

It should behave more like:

- “understand the objective, model the work, choose one safe slice, execute it, learn from the result, then choose the next slice”

### 2. Atomicity Must Be Explicit

Atomicity cannot remain an implicit vibe or an after-the-fact judgment.

The harness repeatedly failed when it allowed broad or self-referential tasks to reach execution without an explicit atomicity contract.

The current working definition for v1 is intentionally strict:

- one file
- at most one class or function target

This is not a claim that all real software changes are naturally one-file operations.

It is a safety-oriented execution policy.

Why this matters:

- it reduces blast radius
- it makes validation easier to scope
- it makes drift obvious
- it makes failures attributable
- it prevents broad control-plane churn from masquerading as progress

When a goal is broader than that, the answer is not to loosen the rule casually.

The answer is:

- preserve the broad objective
- refine it into many atomic plan slices
- execute those slices as staged tasks

Examples:

- a distributed rename is not one atomic task
- a signature migration is not one atomic task
- a broad application build should become a sequence of atomic slices

### 3. Broad Objectives Are Normal

Users, especially technical OpenHands-style users, usually start with a broad outcome.

Typical input is closer to:

- “build me an app that does X”
- “add end-to-end capability Y”
- “fix this workflow completely”

Typical input is not:

- symbol-level implementation slices
- pre-authored atomic tasks
- explicit validation mode selection

So the harness must assume broad top-of-funnel input is normal.

This means:

- the first system responsibility is decomposition, not execution
- the user should not have to author dozens of atomic tasks manually
- broad starting input is not a misuse of the product

### 4. Objective Intake Is The Real Top Of Funnel

Before planning, the harness needs intake.

A high-quality system cannot move directly from a broad prompt to execution.

It must first construct a lightweight, explicit understanding of:

- who the users are
- what workflows matter
- what external systems or APIs exist
- what data sources and sources of truth exist
- what deployment constraints matter
- what security or auth constraints matter
- what is MVP vs later scope

This does not need to be a long interrogation, but it must exist.

A practical structure is:

- `Known from prompt`
- `Assumed for now`
- `Needs confirmation later`

The architecture diagram and comprehensive plan should be generated from that intake packet, not directly from raw prompt text.

### 5. Comprehensive Planning And Atomic Execution Are Different Layers

One “plan” object is not enough.

The system needs at least two planning layers:

1. `Comprehensive objective plan`
   - broad
   - covers the whole objective
   - not directly executable

2. `Atomic execution slice`
   - narrow
   - machine-checkable
   - directly mappable to one task

This distinction matters because atomicity constraints should not apply to the comprehensive plan.

They should apply only to the candidate atomic slice.

The broad plan is for understanding.

The atomic slice is for execution.

### 6. Every Task And Run Should Map Back To An Objective

The harness became much easier to reason about once it was clear that:

- objectives can be broad
- plans can be revised
- tasks should come from approved plan slices
- failed runs still contribute useful evidence

That implies a required lineage:

- `objective -> plan -> task -> run`

This lineage matters because the brain should later be able to inspect:

- the original objective
- all plan revisions
- all approved slices
- all derived tasks
- all failed and successful runs
- current repository state

That is how the system can determine:

- whether the objective is complete
- what remains unresolved
- how important the remaining work is
- how difficult it is likely to be

### 7. Execution Quality Depends More On Plan Quality Than Raw Model Quality

Repeated harness failures showed that the main bottleneck was often not:

- “the model is bad”

It was:

- the task was too broad
- the task was self-referential
- the selected validation surface did not match the intended change
- retries were repeating a bad plan shape

In other words:

- a mediocre model with a good plan and bounded slice can make progress
- a strong model with a bad task shape will still waste time

So the system’s leverage is in:

- better planning
- better slice selection
- better validation fit
- better detection of drift

More than in:

- squeezing small gains out of code generation prompts alone

### 8. Validation Must Not Monopolize The Control Loop

The synchronous flow:

- plan
- edit
- compile/test
- analyze
- decide

caused the harness to waste large amounts of time waiting on deterministic validation.

This produced several failures:

- slow or hung validation blocked throughput
- the strategy loop overfit to one blocking task
- repair tasks timed out under validation suites that were too broad

That is why `Split-Phase Execution` matters.

The long-term architecture should treat:

- candidate generation
- deterministic validation
- decisioning

as separate phases with separate scheduling semantics.

### 9. The Harness Needs Honest Data Products

The strongest improvements came from adding honest telemetry and explicit policy outputs rather than more hidden heuristics.

Examples:

- timeout telemetry
- atomicity telemetry
- gate scores and flags
- explicit failure categories
- visible supervisor progress

This mirrors the lesson from `Routellect`:

- good control depends on honest data
- policy can be tuned later if raw evidence is preserved
- missing telemetry cannot be reconstructed afterward

The system should therefore over-collect structured, cheap, explainable data at decision points.

### 10. Recursion Is Inevitable, So It Must Be Visible

The harness now has recursion in multiple places:

- planning refinement
- retry loops
- follow-on generation
- decomposition
- objective completion review

That means operators need a control-flow view, not just a flat list of tasks.

A key product insight is that the UI should likely be built around:

- `Objectives` as the primary object
- Mermaid-style control-flow visualization whenever recursive or branching logic appears

This is not cosmetic.

It is necessary for understanding:

- why the system is doing what it is doing
- whether decomposition is legitimate or churn
- whether the current state is healthy or pathological

### 11. Objective-Centered UX Is The Right Top-Level Product Shape

The product should likely revolve around objectives, not raw tasks.

Why:

- business users think in outcomes, not tasks
- technical users also usually begin with broad outcomes
- tasks are too low-level to explain overall progress
- plans and runs only make sense in the context of an objective

A good UI shape likely includes:

- `Objective view`
  - broad goal
  - intake packet
  - comprehensive plan
  - current completion judgment

- `Control-flow view`
  - plan/task/run lineage
  - recursive loops and retries
  - decomposition chains

- `Evidence view`
  - artifacts
  - diffs
  - validations
  - gate outputs
  - failure categories

### 12. Local-First, Hosted-Ready Is The Right Architecture Pattern

The current product runs locally on a developer machine, but that should be treated as one deployment mode, not the core product assumption.

The open-source architecture should be:

- local-first for ease of adoption
- hosted-ready at the control-plane boundaries

Recommended shape:

- core library
- API server
- web app
- pluggable worker/runtime adapters
- local SQLite mode now
- Postgres/object storage/hosted auth later

This prevents a future multi-tenant rewrite from requiring a product re-architecture.

## Most Important Product Truths

If only a few ideas survive, they should be these:

1. planning is the product
2. atomicity must be explicit
3. broad objectives are normal
4. objective intake is the top of funnel
5. comprehensive planning and atomic execution are different layers
6. every task and run must remain linked to the objective
7. execution quality depends more on plan quality than on raw model quality

## Things To Avoid Repeating

The harness should avoid returning to these failure modes:

- broad tasks presented directly as executable work
- self-referential control-plane tasks without special handling
- validation suites that are much broader than the intended slice
- retries that repeat the same bad task shape
- hidden recursion and follow-on churn
- product decisions made without preserved telemetry
- flat task-only UX that hides objective-level truth

## What This Should Change Immediately

Future work should bias toward:

- objective intake flows
- high-level architecture diagram generation
- red-team passes over diagrams and plans
- comprehensive objective planning
- atomic slice selection and validation
- plan-to-task lineage
- objective-centered web UI

Future work should bias away from:

- treating task execution loops as the main product
- relying on post-hoc retries to rescue bad planning
- assuming users will start with atomic task definitions
