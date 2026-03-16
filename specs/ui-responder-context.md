# UI Responder Context

This spec defines the backend contract for the Harness UI conversation surface.

The goal is to stop answering operator messages with shallow keyword rules and
instead answer from:

1. deterministic current state from the harness
2. recent conversation turns
3. optional retrieved memory from Open Brain or a compatible context backend

## Purpose

The operator-facing UI should feel like one conversation:

- one transcript
- one input box
- one clear next action

The responder is responsible for turning structured state into a useful
operator-facing answer.

## Source Of Truth

The responder does not replace the harness store.

Canonical state remains in:

- harness SQLite records
- durable artifacts
- Mermaid artifacts
- context records

Open Brain or another context backend augments retrieval. It does not define the
current task, run, or objective state.

## Responder Inputs

Each UI message should build one `ResponderContextPacket`.

### Deterministic State

- project
- current objective
- current intent model
- current Mermaid artifact
- current execution gate
- latest linked task
- latest linked run
- readable run artifact summaries
- current next action

### Conversation State

- recent operator messages for the same objective
- recent harness replies for the same objective
- current message

### Signals

- frustration inferred from the current operator message
- recent frustration records for the same objective

### Retrieved Memory

Optional retrieval layer, initially empty by default:

- prior operator corrections
- prior frustration patterns
- prior investigation findings
- lessons learned
- similar run failures
- intent drift notes

The retrieval provider should be pluggable so the local harness can run without
an external dependency, while still allowing Open Brain to augment the packet.

## Packet Shape

The responder packet should include at least:

- `project_id`
- `project_name`
- `objective`
- `task`
- `run`
- `next_action`
- `recent_turns`
- `frustration_detected`
- `retrieved_memories`

The packet should be explicit and typed. It should not be assembled ad hoc
inside the responder itself.

## Responder Output

Each call should produce one `ResponderResult` with:

- `reply`
- `recommended_action`
- `evidence_refs`
- `mode_shift`

### Reply

Plain-language answer for the operator.

### Recommended Action

Examples:

- `answer_prompt`
- `start_run`
- `review_run`
- `open_investigation`
- `revise_mermaid`
- `revise_intent`

### Evidence Refs

Optional pointers to the supporting evidence used for the answer.

Examples:

- latest run id
- artifact labels
- objective id

### Mode Shift

Optional UI or workflow shift.

Examples:

- `none`
- `investigation`
- `mermaid_review`

## Initial Responder Behavior

The first responder implementation may still be deterministic, but it must be
driven by the packet instead of direct keyword checks against raw storage.

This means:

- interpret the message
- inspect current packet state
- answer using current run/objective evidence
- use recent conversation turns for short follow-ups like `how?`

## Open Brain Integration Boundary

Open Brain belongs between:

- packet builder
- responder

Flow:

1. build deterministic packet from harness state
2. query retrieval provider with the current objective, message, and recent turns
3. attach retrieved memories to the packet
4. generate the response

This keeps:

- exact workflow truth in the harness
- long-term relevant memory in Open Brain

## Non-Goals

The responder is not:

- a shell runner
- a replacement for the harness CLI
- a replacement for the workflow engine

It is the conversational explanation and control surface over existing harness
state.
