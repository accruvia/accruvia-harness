# Plan To Task Mapping

## Purpose

`Plan To Task Mapping` defines how an approved atomic plan becomes an executable task and how all downstream runs remain traceable back to the original objective.

This is the missing linkage that allows the brain to study:

- original objective
- approved plan slices
- tasks created from those slices
- successful and failed runs
- current repository state

With this linkage, objective-completion reasoning becomes grounded in real attempted work rather than only current backlog state.

It also allows broad goals to remain broad while execution stays atomic.

## Core Mapping

The intended lineage is:

- one `objective`
- many `plans`
- each approved `plan` may create one `task`
- each `task` may create many `runs`

Required invariants:

- every task must map to exactly one approved plan
- every approved plan must map to exactly one objective
- every run must map to exactly one task
- failed runs do not break lineage; they enrich it

## Required Identifiers

Tasks should carry:

- `objective_id`
- `plan_id`
- `parent_plan_id`
- `plan_revision_number`
- `atomicity_definition_version`

Runs should carry:

- `objective_id` via task lineage
- `plan_id`
- `source_task_id`
- `source_run_id` when applicable

Artifacts and evaluations should remain queryable by:

- objective
- plan
- task
- run

## Task Creation Rules

A task may be created only from a plan whose validator decision is:

- `approve_plan`
- or `approve_with_warning`

Task creation should copy the plan’s execution-facing fields:

- title
- objective summary
- allowed paths
- forbidden paths
- validation mode
- required tests or validation bundle
- targeted file and symbol metadata

## Task Title And Objective

Task titles and objectives should be derived from the approved plan slice, not only the top-level objective.

That means a broad objective such as:

- `Build me an app`

must never directly become one task title.

Instead, the first approved atomic plan slice might produce a task like:

- `Add health-check handler in server bootstrap`

This makes backlog and run history much more interpretable.

For broad engineering programs, the mapping should look like:

- one coordination objective
- many approved atomic plans
- many tasks, each representing one safe slice

This is especially important for:

- distributed renames
- signature migrations
- staged compatibility work

## Plan Drift Recording

After execution, the harness should compare approved plan vs actual run output.

At minimum, it should record:

- planned files vs changed files
- planned symbol vs modified symbols
- allowed paths vs actual paths
- declared validation mode vs actual validation mode used

This allows each run to say:

- aligned to plan
- partially aligned
- drifted from plan

That signal should influence:

- retry logic
- follow-on creation
- objective completion analysis

## Objective Completion Analysis

When the brain later studies whether an objective is complete, it should review:

- original objective
- all plan revisions for that objective
- all approved plans
- all tasks derived from those plans
- all runs and outcomes
- latest repo state
- latest version-controlled code

This lets the brain distinguish:

- finished objective
- partially completed objective
- abandoned but still important objective
- low-priority unresolved objective
- unresolved objective blocked by poor plan quality

## Difficulty And Importance Signals

The objective-completion analysis should consider:

- how many plans were required
- how many plans failed validation
- how many tasks failed despite valid plans
- whether unresolved portions remain high-impact
- whether unresolved slices are now easier or harder given current repo state
- whether remaining unresolved work is merely coordination overhead or still conceptually unclear

This is why preserving plan lineage matters.

Without it, the brain only sees task churn and current code, not the structure of attempted work.

## Follow-On Semantics

Follow-on tasks should not lose plan lineage.

If a run fails:

- the new follow-on task should either:
  - reference the same plan slice with revised execution metadata
  - or create a new child plan revision if the failure implies the plan itself was wrong

This distinction matters:

- execution failure is not the same as planning failure
- plan drift is not the same as repo bug complexity

## Queryability

The system should support queries such as:

- all plans for objective `X`
- all tasks created from plan `Y`
- all failed runs for objective `X`
- all unresolved approved plans
- all plans rejected as non-atomic

This should eventually surface in summary, ops, and brain context packets.

## Non-Goals

This spec does not define:

- the full storage migration
- exact SQL schema
- complete UI or CLI surfaces

It defines the control-plane mapping contract:

- objective -> plan -> task -> run
