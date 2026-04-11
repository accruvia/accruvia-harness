# Atomic Plan Schema

## Purpose

`Atomic Plan Schema` defines the structured planning object that sits between a high-level objective and an executable task.

The schema exists to make atomicity explicit before code generation begins.

The main rule for the first version is strict:

- one target file
- at most one class or function target within that file

If a proposed plan exceeds that boundary, it is not atomic and must be refined before task execution.

This strictness is intentional because one-file plans are safer by default:

- they reduce signature-change blast radius
- they prevent distributed renames from sneaking into one task
- they make validation and drift easier to understand
- they force staged migration instead of broad refactor churn

## Why Plans Must Be First-Class

The harness currently stores:

- objectives
- tasks
- runs
- artifacts

That is not enough to reason cleanly about whether a failed attempt actually addressed the intended slice.

A structured plan lets the harness answer:

- what exact slice was intended
- whether the slice was atomic
- whether the worker drifted from the plan
- whether the objective is complete across multiple attempts

## Hierarchy

The intended hierarchy is:

1. `objective`
   - a product or engineering goal
   - may require many plans and many tasks

2. `plan`
   - a proposed implementation slice against an objective
   - must be machine-inspectable
   - may be revised before execution

3. `task`
   - an executable unit derived from an approved plan slice

4. `run`
   - one concrete attempt to implement a task

This means:

- objectives are not required to be atomic
- plans are the place where atomicity is checked
- tasks inherit atomicity from approved plans

Broad goals should become many approved plan slices, not one broad task.

## Required Fields

Minimum fields for the first schema version:

```json
{
  "schema_version": 1,
  "objective_id": "objective_123",
  "objective_title": "Allow project-name targeting in supervise",
  "objective_summary": "Accept stable project names anywhere project ids are currently required.",
  "plan_id": "plan_123",
  "parent_plan_id": null,
  "source_task_id": null,
  "source_run_id": null,
  "atomicity_definition_version": 1,
  "proposed_slice": {
    "files": [
      {
        "path": "src/accruvia_harness/commands/core.py",
        "symbols": [
          {
            "kind": "function",
            "name": "_resolve_project_ref",
            "summary": "Accept stable project name as an alternative to project id."
          }
        ],
        "work_summary": "Add project-name resolution for supervise entrypoints."
      }
    ]
  },
  "constraints": {
    "allowed_paths": [
      "src/accruvia_harness/commands/",
      "tests/test_cli.py",
      "tests/test_phase1.py"
    ],
    "forbidden_paths": [
      "src/accruvia_harness/services/",
      "src/accruvia_harness/skills_worker.py"
    ],
    "max_files": 1,
    "max_symbols": 1
  },
  "atomicity_assessment": {
    "is_atomic": true,
    "violations": [],
    "reason": "Touches one file and one function only."
  },
  "planner_notes": "Narrow operator-surface slice only.",
  "created_at": "2026-03-13T12:00:00Z"
}
```

## Proposed Slice

The core planning payload is `proposed_slice`.

Rules for schema v1:

- `files` must contain exactly one file for an atomic plan
- each file may list zero or one symbol target
- `symbols` may include only one entry for schema v1 atomic execution
- `kind` is expected to be one of:
  - `function`
  - `method`
  - `class`
  - `module`

`module` is allowed only when the change is limited to one file and no narrower symbol can be identified safely.

## Constraints

Plans must carry explicit constraints so later worker drift can be measured.

Initial constraints:

- `allowed_paths`
- `forbidden_paths`
- `max_files`
- `max_symbols`

Optional future constraints:

- `forbidden_surface_classes`
- `required_tests`
- `validation_mode`
- `must_not_modify_existing_public_symbols`

## Atomicity Contract

The first contract is intentionally strict.

An atomic plan passes only if:

- `file_count <= 1`
- `symbol_count <= 1`
- no forbidden path is present
- no forbidden control-plane surface is present unless explicitly allowed

This contract is opinionated by design.

The harness should treat this as the default safe operating mode, not as a claim that all software changes are naturally one-file operations.

When a goal truly spans many files, the expectation is:

- preserve the broad objective
- refine it into many atomic plan slices
- execute those slices as staged tasks

If later versions loosen it, the schema must still preserve:

- declared limits
- deterministic validator results

## Plan Revision

Plans are expected to be revised before task execution.

Each revised plan should preserve:

- `objective_id`
- `parent_plan_id`
- revision lineage
- atomicity assessment history

This allows the brain to reason about:

- what decompositions were attempted
- which slices succeeded
- which slices failed
- what remains unresolved for the same objective

## Drift Detection

After execution, the harness should compare:

- planned file paths vs actual changed files
- planned symbol targets vs actual modified symbols
- planned constraints vs actual diff

This enables:

- `plan_alignment_score`
- `plan_drift_detected`
- exact reporting of where the worker diverged

## Non-Goals

This schema does not try to:

- describe full project architecture
- replace task lineage
- encode every possible dependency relationship in v1

Its job is narrower:

- make atomic slices explicit
- make them machine-checkable
- preserve plan lineage against an objective
