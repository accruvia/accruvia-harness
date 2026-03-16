# Atomic Plan Validator

## Purpose

`Atomic Plan Validator` is the deterministic policy that decides whether a structured plan is atomic enough to become an executable task.

The validator is intentionally authoritative.

The LLM may propose or revise plans, but the harness decides whether a plan satisfies the atomicity contract.

## Placement

The validator runs:

1. after the planner proposes a structured plan
2. before any task is created from that plan
3. before any worker is asked to edit code

This is earlier than the existing attempt-time atomicity gate.

The current attempt-time gate remains useful as a backstop, but the plan validator should prevent many bad tasks from being created at all.

## Inputs

The validator consumes:

- structured plan JSON
- objective metadata
- project metadata
- surface classification rules
- optional repository metadata

## Outputs

The validator must emit structured results:

```json
{
  "schema_version": 1,
  "is_atomic": false,
  "violations": [
    "too_many_files",
    "too_many_symbols",
    "forbidden_path_touched"
  ],
  "score": 7,
  "flags": [
    "multi_file",
    "multi_symbol",
    "control_plane_path"
  ],
  "decision": "revise_plan",
  "rationale": "Plan exceeds one-file/one-symbol atomicity contract."
}
```

The validator must not emit only a boolean.

At minimum it should return:

- `is_atomic`
- `violations`
- `flags`
- `decision`
- `rationale`

## Deterministic Rules For V1

Initial hard rules:

- fail if `proposed_slice.files` length is greater than `1`
- fail if total targeted symbols across all files is greater than `1`
- fail if any file is outside `allowed_paths`
- fail if any file is within `forbidden_paths`
- fail if self-hosting operator slices touch control-plane files not explicitly allowed

Initial soft risk points:

- `+2` if targeted file is in a control-plane surface
- `+2` if targeted file is in validation-policy code
- `+1` if plan summary uses broad terms without specific file or symbol targets
- `+1` if objective suggests operator UX work but plan targets services or workers
- `+1` if the plan omits symbol targets and uses `module` scope

Suggested initial decisions:

- no hard violations and score `0-1`: `approve_plan`
- no hard violations and score `2-3`: `approve_with_warning`
- any hard violation or score `>= 4`: `revise_plan`

## Self-Hosting Specialization

The validator should support repo-specific policy overlays.

For `accruvia-harness`, additional deterministic checks should exist for:

- validation machinery paths
- retry/decomposition logic
- supervisor semantics
- task creation and task selection code
- cognition or heartbeat planning code

This keeps the general mechanism portable while allowing self-hosting control-plane protections.

## Coordination Objectives

The validator should not respond to broad but legitimate software goals by weakening the atomicity rule.

Instead, when a plan is non-atomic because the objective is broad, the validator should push it back into refinement as a coordination problem.

Examples:

- rename a widely used function
- migrate a function signature across callers
- stage a compatibility transition

The expected outcome is:

- one broad objective
- many approved atomic plans
- many tasks derived from those plans

This keeps the validator simple and keeps execution slices safe.

## Feature Extraction

The validator should extract, at minimum:

- targeted file count
- targeted symbol count
- file surface classes
- allowed/forbidden path violations
- self-hosting control-plane touch
- declared validation mode
- objective-to-surface mismatch

These features should also be recorded as telemetry, because plan-time atomicity is a data product too.

## Decision Types

Initial plan-validator decisions:

- `approve_plan`
- `approve_with_warning`
- `revise_plan`
- `reject_plan`

`reject_plan` should be used sparingly for invalid or contradictory plans.

Most non-atomic plans should receive `revise_plan`, not hard rejection.

## Relationship To Attempt-Time Atomicity Gate

The plan validator and attempt-time atomicity gate solve different problems.

Plan validator:

- before task creation
- uses planned files and symbols
- prevents obviously bad tasks from being created

Attempt-time gate:

- after code generation
- uses actual changed files and diff shape
- detects worker drift or post-plan broadening

Both should exist.

## Auditability

The validator result should be stored with:

- objective id
- plan id
- parent plan id
- validator version
- rule results
- score and flags
- decision

This history matters because later success or failure must be interpreted against what the harness believed at planning time.

## Non-Goals

The v1 validator does not need:

- AST-perfect symbol resolution in every language
- fuzzy semantic scoring as a primary decision maker
- learned weights

The first goal is:

- deterministic, explainable, early atomicity enforcement
