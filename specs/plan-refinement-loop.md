# Plan Refinement Loop

## Purpose

`Plan Refinement Loop` is the bounded control loop that improves a non-atomic plan before any executable task is created.

The loop exists because high-level objectives may require many tasks, but the first plan produced by the planner LLM may still be too broad.

Instead of turning a broad objective directly into a bad task, the system should refine the plan until it satisfies the atomicity contract or until refinement is exhausted.

## Core Principle

Decompose plans before spending execution budget.

This loop is about plan quality, not code-generation retries.

## Inputs

Each refinement round consumes:

- the original objective
- the current structured plan
- the latest validator result
- prior revision history
- repo-specific surface constraints

## Required LLM Output

The refinement prompt should require structured output, not free-form text.

Minimum response contract:

```json
{
  "schema_version": 1,
  "is_improvable": true,
  "improvement_summary": "Reduce the slice to one CLI entrypoint helper and its direct test.",
  "violations_acknowledged": [
    "too_many_files",
    "control_plane_path"
  ],
  "revised_plan": {
    "...": "Atomic plan schema payload"
  },
  "confidence": 0.78
}
```

`true/false` alone is not enough.

The harness needs:

- whether the LLM sees room to improve
- what it thinks is wrong
- a revised plan object

## Loop Semantics

Recommended v1 loop:

1. create initial plan
2. validate plan deterministically
3. if approved, stop and create task
4. if not approved:
   - ask the planner to improve the plan under the validator feedback
   - validate the revised plan
   - repeat for a bounded number of rounds
5. if no valid atomic plan emerges:
   - record planning failure explicitly
   - optionally create a higher-level decomposition or operator review item

## Bounds

The loop must be bounded by convergence, not by a fixed universal round count.

The harness should allow long planning sessions when the plan is still materially improving.

The loop should continue while one or more of these signals improve:

- validator violations decrease
- targeted file count decreases
- targeted symbol count decreases
- forbidden/control-plane surface touches are removed
- objective-to-surface alignment improves
- the revised plan becomes more specific and less module-wide

The loop should stop when it plateaus or cycles, for example:

- identical validator results repeat without structural improvement
- the same plan payload reappears with only cosmetic wording changes
- confidence rises while violations remain unchanged
- the planner keeps drifting into the same forbidden/control-plane surfaces

This is still bounded behavior, but the bound is evidence-driven rather than an arbitrary small round count.

## Improvement Question

The refinement prompt should be concrete.

Recommended framing:

`This plan does not satisfy the atomicity contract. Improve the plan so it touches no more than one file and one class/function, stays within allowed paths, and avoids forbidden/control-plane surfaces unless explicitly required.`

The prompt should include:

- original objective
- current plan JSON
- validator violations
- validator flags
- allowed paths
- forbidden paths

## Stored Revision History

Each refinement round should preserve:

- `objective_id`
- `plan_id`
- `parent_plan_id`
- `revision_number`
- validator result
- planner response
- whether the plan improved

This makes planning quality auditable.

## Success Criteria

A plan refinement loop succeeds when:

- a plan passes the deterministic validator
- the plan remains meaningfully aligned with the original objective
- the plan is specific enough to become one executable task

## Failure Criteria

The loop fails when:

- refinement rounds are exhausted
- the planner repeatedly proposes non-atomic plans
- the planner repeatedly drifts into forbidden/control-plane surfaces
- the revised plan stops addressing the original objective meaningfully

Failure should be recorded as a planning outcome, not disguised as a worker failure.

## Broad Objectives And Coordination Work

Some objectives are legitimately larger than one atomic task.

Examples:

- distributed function rename
- signature migration across many callers
- interface hardening with compatibility rollout

These should not weaken the atomicity contract.

Instead, they should become `coordination objectives`:

- the objective remains broad
- the refinement loop produces one atomic slice at a time
- the approved slices become staged tasks in a migration sequence

This means a broad change is handled as a program of atomic tasks, not as one broad task.

## Relationship To Task Splitting

Task splitting should happen after planning, not instead of planning.

The intended sequence is:

- broad objective
- refine to atomic plan
- create one task from approved plan
- if many approved plans are needed, create many associated tasks

This is different from repeatedly splitting tasks after failed execution.

## Role Of LLM Review

The refinement loop may use the LLM to improve plans, but deterministic validation remains the authority.

Good division of labor:

- LLM:
  - proposes a revised narrower slice
  - explains how the plan improved

- harness:
  - checks whether the plan really is atomic
  - decides whether to continue refining or stop

## Non-Goals

The loop does not try to:

- solve execution drift after code generation
- replace attempt-time atomicity checks
- guarantee perfect decomposition in one round

Its job is narrower:

- stop broad tasks from being created too early
