# Engineering Checklist

This checklist is for reviewing `accruvia-harness` as a product and as a workflow control system.

## Architecture & Flexibility

- Is the workflow engine separate from task-source integrations such as GitLab?
- Can we replace GitLab with another task source without rewriting the core engine?
- Can we replace the worker implementation without changing task, run, artifact, evaluation, and decision records?
- Is execution truth explicitly internal to the harness rather than spread across chat history, issue comments, and scripts?
- Are retries, promotions, failures, and branching represented as explicit policy decisions?
- Are queue selection and prioritization separate from worker execution logic?
- Is the event history good enough to replay why a task was retried, promoted, failed, or branched?
- Are artifacts first-class records rather than implied by logs or conversations?
- Is the data schema flexible enough to add new task metadata, artifact metadata, and evaluation outputs without a rewrite?
- Are external integrations isolated behind adapters rather than embedded throughout the engine?
- What is the single most difficult part of the codebase to change right now?

## Velocity & Onboarding

- Can a new engineer get the project running locally in under 30 minutes?
- Is the setup documented from zero to first successful run?
- Can an engineer initialize the database and run the happy path with one or two commands?
- Can an engineer understand the workflow loop from one primary document?
- Is the source of truth for architecture and workflow logic clearly documented?
- Is there a one-command test run?
- Is there a one-command local smoke test for a full task lifecycle?
- Is there a one-command way to import tasks from an external source?
- How long does the full build and test cycle take?
- Is there a deployment story, even if only for staging or local orchestration?
- What is the current bus factor for the workflow core?

## Risk & Quality

- Do we have automated tests for the happy path?
- Do we have automated tests for retry and failure paths?
- Do we have automated tests for external integrations using mocks or fixtures?
- Do we have explicit tests for idempotency when importing external tasks?
- Are errors captured in a form that is actionable for debugging?
- Is event history rich enough to diagnose bad decisions after the fact?
- Do we have a strategy for schema migrations?
- Do we have a strategy for versioning the CLI or API surface?
- Do we have a strategy for evolving evaluation logic without breaking old runs?
- What are the top three technical shortcuts currently in use?
- What production risks exist because the planned architecture is not fully implemented yet?

## Product Readiness

- Can the harness manage more than one project without hidden assumptions?
- Can it support parallel work safely?
- Can it distinguish external task identity from internal execution identity?
- Can it report results back to external systems without making them the control plane?
- Can it explain overall productivity and throughput over time?
- Can it support follow-on task generation without corrupting the original task lineage?
- Can it promote work based on explicit evaluation rather than optimistic success claims?
- Can it reject incomplete candidate outputs reliably?
- Can it support long-running workflows without manual babysitting?
- What is the next bottleneck to scaling from prototype to dependable system?

## Current Assessment Template

- `green`: working and documented
- `yellow`: partially implemented or credible but incomplete
- `red`: absent, fragile, or unclear

For each review, capture:

- current score
- evidence
- owner
- next action
- target date
