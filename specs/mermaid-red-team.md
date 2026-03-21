# Mermaid Red Team

## Goal

Create a repeatable pre-human review process that attacks a Mermaid diagram the
way an exacting operator would attack it in review.

The objective is not visual polish. The objective is to eliminate ambiguous,
dangerous, or architecturally wrong readings before a human reviews the design.

## When To Use It

Use this process for any Mermaid that influences:

- execution control
- context management
- retry logic
- planning
- state transitions
- persistence boundaries
- operator UX for control paths

Do not use this only for final polish. Use it before the diagram is treated as
reviewable.

## Core Principle

A Mermaid is not good enough when it is internally coherent.

A Mermaid is good enough when a hostile implementer, a rushed implementer, and a
literal implementer all arrive at the same intended architecture.

## Review Loop

Run this loop until there are no unresolved findings above the accepted
severity threshold:

1. Generate or update the Mermaid.
2. Red-team the Mermaid with the rubric below.
3. Record findings in severity order.
4. Patch the Mermaid and nearby spec text to remove the finding, not just soften
   the language.
5. Add or tighten explicit invariants when the diagram alone is too easy to
   misread.
6. Re-run the red team review from scratch.
7. Only hand to a human reviewer when no major ambiguity remains.

## Required Red Team Questions

Every Mermaid review must answer these questions explicitly:

1. Does the diagram match the actual operator intent, or did it drift broader or
   narrower than intended?
2. Does it accidentally block planning, investigation, or UX flows that should
   still work with partial information?
3. Does it blur read and write boundaries?
4. Does it blur canonical state, retrieved memory, and inferred signals?
5. Does it imply a god service or hidden ownership transfer?
6. Does it imply the wrong primary key or scope such as objective-only when the
   design is also project or operator scoped?
7. Could an implementer satisfy the diagram while violating the architecture?
8. Are any branch labels broad enough that a future implementer could gate the
   wrong thing?
9. Does the diagram show control order correctly, especially around assembly,
   gating, mutation, and rebuild?
10. If the diagram were implemented literally, what is the most likely wrong
    implementation?

If any answer reveals a plausible wrong implementation, the Mermaid is not
ready.

## Failure Classes

Classify findings into these buckets:

### Intent Drift

The Mermaid does not match the settled objective.

Examples:

- centralizing more ownership than intended
- narrowing scope to objectives when the design includes project scope
- implying automation when the design is operator-mediated

### Flow Blocking Error

The Mermaid blocks a flow that should degrade gracefully.

Examples:

- planning blocked by execution checks
- packet construction blocked by missing execution artifacts
- investigation blocked when canonical state is incomplete

### Boundary Blur

The Mermaid collapses distinct responsibilities.

Examples:

- read assembly and persistence mixed together
- retrieval shown as if it mutates canonical state
- mutation shown as a mode of a read-only service

### Control Ambiguity

The Mermaid can be read in more than one materially different way.

Examples:

- labels like `ready?` without specifying readiness for what
- unlabeled rebuild edges after mutation
- caller nodes that hide materially different control paths

### Ownership Inflation

The Mermaid makes one service look responsible for too much.

Examples:

- a packet service that also becomes planner, recorder, and gate authority
- a store-shaped service disguised as a context service

### Implementer Trap

The Mermaid is technically true but easy to implement incorrectly.

Examples:

- sequence is implied but not stated
- optional augmentation looks required
- partial packet semantics are left unstated

## Severity

Use these severities:

- `critical`
  The diagram would likely cause the wrong architecture or unsafe control
  behavior.
- `major`
  A competent implementer could plausibly make the wrong decision from the
  diagram.
- `minor`
  The architecture is still likely to survive, but wording or structure could
  mislead.
- `nit`
  Cosmetic or readability issue with no meaningful architectural risk.

Human review should not start while any `critical` or `major` finding remains.

## Patch Rules

When a finding is discovered:

- patch the Mermaid first if the flaw is structural
- patch the surrounding spec text if the Mermaid needs interpretation support
- add an explicit contract if the diagram cannot safely carry the meaning alone
- do not accept “the prose elsewhere makes it obvious” as a sufficient fix for a
  major control-path ambiguity

## Execution Contract Requirement

Any Mermaid that governs execution or context assembly should include an
`Execution Contract` or equivalent invariant block immediately after the
diagram.

That contract should state:

- what is read-only
- what is the normalized write boundary
- what happens before gating
- what can proceed with partial information
- what is additive only
- what must remain explicit rather than implicit

## Suggested Reviewer Prompts

Use prompts like these for the red team pass:

- `What is the most dangerous incorrect implementation this Mermaid permits?`
- `Where does this diagram accidentally over-centralize ownership?`
- `What would a rushed implementer gate that should not be gated?`
- `Which node label is broad enough to be misread?`
- `What flow is missing if the operator only has partial artifacts?`
- `What boundary is still too blurry to implement safely?`
- `Does mutation appear as a consumer mode when it should be a companion boundary?`
- `What scope does the entrypoint imply, and is that the intended scope?`

## Completion Standard

A Mermaid is ready for human review only when:

- the red team cannot produce a `critical` or `major` finding
- the most likely wrong implementation is explicitly prevented
- the control order is unambiguous
- read/write boundaries are explicit
- canonical vs retrieved vs inferred distinctions are preserved
- the diagram has a local invariant block when the flow is architecturally
  important

## Applying This To Context Management

For the context-management Mermaid, the red team should be especially aggressive
about:

- planning accidentally blocked by execution-artifact checks
- `ContextService` turning into a god object
- `ContextRecorder` being shown as a mode of `ContextService`
- objective-only framing when project and operator scopes also matter
- retrieval looking canonical
- mutation and rebuild order being implicit

If any of those survive, the Mermaid is not ready.
