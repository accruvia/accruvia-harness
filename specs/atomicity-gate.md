# Atomicity Gate

## Purpose

`Atomicity Gate` is a deterministic pre-validation policy that uses attempt telemetry to decide whether a candidate attempt should:

- validate normally
- validate with a narrower suite
- decompose before expensive validation
- be blocked as self-referential control-plane work

The goal is to stop obviously non-atomic or self-defeating attempts from wasting validation time.

## Placement In The Run Pipeline

The gate runs:

1. after candidate generation
2. after changed-file inventory is known
3. after attempt telemetry features are collected
4. before compile and deterministic validation

## Inputs

The gate consumes:

- raw atomicity telemetry features
- task metadata
- retry history
- project context
- surface-classification flags

## Outputs

The gate emits:

- `atomicity_risk_score`
- `atomicity_flags`
- `gate_action`
- `gate_rationale`

These outputs should be stored as:

- structured event payloads
- run details / report fields
- telemetry features for later tuning

## Initial Actions

The first version supports four actions:

### `validate_normal`

Use the configured validation policy for the task.

### `validate_narrow`

Switch to a narrower deterministic validation slice that better matches task intent and changed surfaces.

### `decompose_first`

Do not spend full validation cost yet. Create a narrower follow-on task or split plan first.

### `block_self_referential`

Do not continue validation. Record that the attempt modified the machinery that evaluates attempts of its own class.

## Initial Heuristic

The first version should be intentionally simple and explainable.

### Risk Points

- `+1` if `changed_file_count >= 4`
- `+1` if `subsystem_count >= 3`
- `+2` if `project_is_self_hosting` and `touches_control_plane`
- `+2` if `touches_validation_policy`
- `+3` if `self_referential_change_detected`
- `+1` if `retry_attempt_number >= 2`
- `+1` if `prior_timeout_count >= 1`
- `+1` if `intent_surface_mismatch_detected`
- `+1` if `touched_files_without_validation_target_count >= 2`
- `+1` if `operator_task_touches_non_operator_surface`

### Immediate Override

If `self_referential_change_detected = true`, the gate may immediately choose `block_self_referential` regardless of the total score.

### Initial Decision Bands

- score `0-1`: `validate_normal`
- score `2-3`: `validate_narrow`
- score `4-5`: `decompose_first`
- score `>= 6`: `block_self_referential`

These bands are a starting point only.

## Why Start Simple

This heuristic is intentionally crude because:

- the first job is to generate honest labeled data
- every decision must be easy to audit
- thresholds can be tuned later
- feature definitions matter more than early coefficient quality

## Deterministic Atomicity Flags

The gate should emit flags, not just a score.

Initial flags:

- `large_diff`
- `wide_surface`
- `control_plane_touch`
- `validation_policy_touch`
- `self_referential_change`
- `retry_pressure`
- `timeout_history`
- `intent_surface_mismatch`
- `validation_scope_mismatch`
- `operator_surface_drift`

These flags are useful both for operator UX and later tuning.

## Deterministic Self-Referential Detection

The first version should detect self-reference without an LLM when possible.

Examples:

- task runs under `validation_mode = lightweight_operator`
- changed files include the code that defines `lightweight_operator`
- task changes retry logic while being retried under that same logic
- task changes supervisor idle/requeue behavior while being processed by that path

This is especially important for self-hosting tasks.

## Narrow Validation Selection

When the gate chooses `validate_narrow`, it should choose from a deterministic set of narrower suites.

Examples:

- `lightweight_operator`
- `lightweight_repair`
- `targeted_store`
- `targeted_cli`

The gate should not blindly trust the worker LLM’s claimed validation command. It may use the worker’s suggested validation as a hint, but the harness remains authoritative.

## Decomposition Semantics

When the gate chooses `decompose_first`, the system should:

- persist the gate decision
- record why the attempt appears non-atomic
- create a narrower follow-on task or split recommendation
- avoid burning broad validation time on the current attempt

## Telemetry And Learning

The gate is expected to evolve.

Future tuning may use:

- weighted heuristic updates
- logistic or bandit-style learning
- evolutionary / genetic tuning over thresholds and weights

That later tuning must consume the same raw telemetry features recorded by `Atomicity Telemetry`.

## Operator Visibility

Operators should be able to see:

- score
- triggered flags
- chosen action
- short rationale

Example:

`Atomicity gate: score 5, flags=[control_plane_touch,self_referential_change,retry_pressure], action=decompose_first`

## Non-Goals

The first gate does not try to:

- solve task planning globally
- replace validation
- make fuzzy semantic judgments in all cases

The first goal is narrower:

- reduce obviously wasted validation time
- identify self-defeating control-plane attempts
- create data for better policy later
