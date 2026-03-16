# Objective UI Redesign

## Purpose

This document resets the local harness UI from a blank-slate perspective.

The goal is not to improve the current screen incrementally. The goal is to
define the correct operator experience for a repo-centered, objective-driven
development system that can eventually run with high autonomy.

The current UI exposes too much of the internal object model:

- objective
- intent model
- Mermaid artifact
- task
- run
- CLI output
- execution gates

Those are real system objects, but they are not the right primary human
surface. The UI currently feels procedural and rigid because the operator is
being asked to navigate records instead of having a guided conversation with a
system that can explain itself.

This redesign treats the following principles as non-negotiable.

## Core UX Principles

### One obvious place to interact

The UI must have one clear primary input surface that means:

- talk to the harness here

That surface should be used for:

- answering clarifying questions
- pushing back
- expressing confusion or frustration
- asking what happened
- asking what happens next

The user should not need to choose between:

- comments
- frustration field
- intent form
- task controls

Those can still exist as structured records internally, but the operator-facing
surface should be one conversation box.

### One next required action

At any moment, the UI should primarily show the single next thing the harness
needs from the operator.

Examples:

- answer the desired outcome question
- confirm success criteria
- review the Mermaid flow
- start the first implementation step
- review the latest run
- revise the plan after investigation

Everything else should be hidden or secondary by default.

### Explain what just happened

The UI must always state:

- what the harness just did
- why it did it
- what it believes the result means
- what it wants next

The operator should never have to infer this from:

- raw JSON
- task status words
- Mermaid status words
- artifact file names

### Save visibly, then advance

When the operator answers a question:

- confirm the exact answer that was saved
- preserve editability
- then advance to the next question or next stage

The transition should feel like guided progress, not like the UI deleted the
operator's words.

### Raw evidence is secondary

The primary surface should not be:

- JSON
- raw CLI output
- execution gate internals
- typed record listings

Those are evidence surfaces, not the main operator experience.

They belong behind:

- drawers
- detail toggles
- investigation mode
- explicit "show everything" expansion

### Frustration is first-class signal

Frustration is not generic commentary.

It is evidence that intent and reality have diverged.

The UI must:

- detect frustration from normal conversation and corrections
- not force the operator into a separate "frustration form"
- translate frustration into triage:
  - likely causes
  - likely divergence category
  - recommendation

### Reuse intent already provided

If the operator already made intent clear in conversation, the UI should not
make them restate it simply because the structured record is missing.

The harness should synthesize and propose structured records from prior
conversation, then ask for confirmation or correction.

## Product Framing

The correct product metaphor is not:

- "fill out system records"

It is:

- "work with a planning partner that remembers context, asks clarifying
  questions, explains what it is doing, and only shows the next thing you need
  to care about"

That means the UI should feel:

- conversational
- progressive
- operator-centered
- evidence-backed

Not:

- admin-panel-like
- database-shaped
- record-first
- status-heavy

## New Top-Level Information Architecture

The local app should be reorganized into five primary modes.

### 1. Objective Inbox

Purpose:

- choose what broad problem to work on

Visible by default:

- objective title
- one-line summary
- current stage
- whether operator attention is needed

Not visible by default:

- tasks
- runs
- raw artifacts
- gate internals

### 2. Guided Conversation

Purpose:

- the main human interface

This should be the primary working view once an objective is selected.

The screen should show:

- one harness message
- one primary input
- optionally one saved-answer receipt
- optional back/expand controls

This replaces the current multi-box intake experience.

### 3. Process Review

Purpose:

- review Mermaid when flow, state, or interaction clarity is required

This mode should show:

- the question
- the Mermaid diagram
- finish / pause / revise controls

It should not simultaneously show:

- unrelated forms
- raw evidence
- unrelated comments

### 4. Execution Review

Purpose:

- explain what bounded step exists now
- let the operator start it or review its latest run

This should show:

- plain-language step title
- why this step exists
- current run state in plain English
- one primary action

Not:

- raw task metadata as the main content

### 5. Investigation

Purpose:

- diagnose divergence when the process feels wrong

This is the one place where richer evidence becomes primary.

It should show:

- likely divergence category
- Mermaid/control-flow view
- explanation of mismatch
- evidence drawer
- code and telemetry links

## Keep / Eliminate / Demote

### Keep as first-class

- objectives
- one primary conversation box
- Mermaid diagrams
- saved answer receipts
- explicit next action
- plain-language explanation of current state
- one primary action button per stage
- operator comments and frustration as typed records internally

### Keep, but demote behind expanders

- raw CLI output
- JSON artifacts
- execution gates
- detailed task metadata
- detailed run metadata
- record history

### Eliminate from the primary surface

- multiple input boxes that compete as "the place to type"
- jargon-heavy labels like `bounded slice` without explanation
- status-only cards that repeat information without directing action
- duplicate recap surfaces
- object-model-first navigation
- empty panels that consume attention without helping the operator act

## Conversational Stage Design

### Stage A: Objective selected

The harness should immediately synthesize:

- what it believes the objective is
- what it still needs clarified

Then it should ask one question.

The operator should not see:

- task lists
- run lists
- CLI output

### Stage B: Intent clarification

The harness should ask one question at a time:

- desired outcome
- success definition
- non-negotiables
- frustration signals if needed

The operator should answer in the primary conversation input.

The harness should then:

- save the answer
- show the saved answer back
- ask the next question

### Stage C: Mermaid review

Once a Mermaid is required, the UI should switch into process review mode.

The operator should see:

- one review prompt
- one diagram
- clear choices:
  - matches my flow
  - doesn't match yet

### Stage D: Execution handoff

Once intent and Mermaid are accepted, the harness should say:

- "I created the first implementation step."
- "Here is why this is the right next step."
- "Do you want me to start it now?"

That is more human than:

- "First bounded slice created"

### Stage E: Run review

After a run exists, the UI should explain:

- what the harness attempted
- whether it succeeded, failed, or is blocked
- what the operator should do next

Examples:

- review result
- approve progression
- revise the plan
- enter investigation mode

It should not force the operator to read raw JSON first.

## Plain-Language Translation Rules

The UI must translate internal objects into operator language.

Examples:

- `objective`
  - "what we're trying to accomplish"

- `intent model`
  - "what success means and what constraints matter"

- `Mermaid artifact`
  - "the current process map"

- `task`
  - "the next implementation step"

- `run`
  - "the latest attempt"

- `blocked`
  - "the harness cannot continue without clarification"

- `failed`
  - "the harness tried and hit a real failure"

The operator can still inspect the internal terms later, but that should not be
the default language.

## Handling Frustration

The UI should infer frustration from:

- repeated corrections
- repeated confusion
- "this makes no sense"
- "what am I supposed to do now?"
- repeated returns to planning
- repeated pushes against jargon or irrelevant information

When frustration is detected, the UI should:

1. acknowledge it directly
2. say it likely has a real cause
3. explain the likely cause category
4. recommend the next mode:
   - clarification
   - Mermaid review
   - investigation
   - plan revision

This should happen in the main conversational surface, not by forcing a
special-purpose frustration form.

## Blank-Slate Screen Model

If we started over today, the main screen after selecting an objective would
look like this:

### Header

- objective title
- one-line summary
- current stage

### Main conversation card

- harness message
- one question or one explanation
- one primary input or one primary action button

### Secondary controls

- back
- show details
- open process map
- open evidence

That is enough for the main workflow.

Everything else should be layered behind it.

## First Redesign Pass

The first serious redesign should do the following:

1. Replace multiple intake boxes with one conversation input.
2. Remove the dedicated frustration form from the primary surface.
3. Replace `Execution Prep` with a conversational handoff card:
   - "I created the next implementation step"
   - "Start it now"
4. Hide CLI output by default until a run is ready for review.
5. Hide raw execution gates by default.
6. Make Mermaid review a dedicated mode with diagram + decision only.
7. Make post-run review a dedicated mode with plain-language summary first.

## Design Decision Rubric

When deciding whether to keep or eliminate a UI element, ask:

1. Does this help the operator know what to do next?
2. Does this reduce ambiguity?
3. Does this explain what just happened?
4. Is this primarily for humans or for the internal object model?
5. Can this move behind a detail drawer without harming the main flow?

If the answer to the first three is "no", the element should not be in the
primary surface.

## North Star

The machine should keep all the structured objects it needs.

The human should mostly experience:

- conversation
- confirmation
- process review when necessary
- one next action
- optional deep evidence

That is the standard for every future UI change.
