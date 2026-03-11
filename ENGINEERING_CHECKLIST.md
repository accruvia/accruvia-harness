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

## Current Assessment (as of Phase 6 completion)

Legend: `green` = working and tested, `yellow` = partially implemented, `red` = absent or fragile

### Architecture & Flexibility

| Question | Score | Evidence |
|---|---|---|
| Engine separate from task-source integrations? | `green` | GitHubTaskService, GitLabTaskService are isolated services; engine delegates |
| Replace GitLab without rewriting core? | `green` | GitHub integration added alongside GitLab with no engine changes |
| Replace worker without changing records? | `green` | WorkerBackend protocol; LocalArtifactWorker, LLMWorker, ProfileAwareWorker all swap cleanly |
| Execution truth internal? | `green` | All state in SQLite: tasks, runs, artifacts, evaluations, decisions, events |
| Retries/promotions/failures/branching as policy? | `green` | DefaultPlanner, DefaultAnalyzer, DefaultDecider with DecisionAction enum (RETRY, PROMOTE, FAIL, BRANCH) |
| Queue selection separate from worker? | `green` | QueueService handles selection/leasing; RunService handles execution |
| Event history for replay? | `green` | Append-only events table with entity_type, entity_id, event_type, payload |
| Artifacts first-class? | `green` | Dedicated artifacts table with run_id, artifact_type, path, description |
| Schema flexible for new metadata? | `green` | Migration-managed schema (6 versions); JSON columns for extensible fields |
| External integrations behind adapters? | `green` | AdapterRegistry, ProjectAdapterRegistry, validation profiles isolate concerns |

**Most difficult part to change**: the evaluation/promotion flow spans RunService, PromotionService, and PromotionValidatorRegistry — changes there touch multiple layers.

### Velocity & Onboarding

| Question | Score | Evidence |
|---|---|---|
| Running locally in 30 minutes? | `green` | `pip install -e .` + `accruvia-harness init-db` + `accruvia-harness smoke-test` |
| Setup documented? | `yellow` | README covers basics; no step-by-step quickstart guide yet |
| Init + happy path in 1-2 commands? | `green` | `init-db` then `smoke-test` |
| Workflow loop from one document? | `green` | PRODUCT_PLAN.md describes the full plan→work→analyze→decide loop |
| Architecture source of truth? | `green` | PRODUCT_PLAN.md + ENGINEERING_CHECKLIST.md |
| One-command test run? | `green` | `python -m pytest tests/` — 92 tests, ~43 seconds |
| One-command smoke test? | `green` | `accruvia-harness smoke-test` |
| One-command task import? | `green` | `import-github-issue` and `import-gitlab-issue` CLI commands |
| Full build + test cycle time? | `green` | ~43 seconds for full suite |
| Deployment story? | `yellow` | Temporal integration exists but requires external Temporal server |
| Bus factor? | `yellow` | Core is well-structured but single-contributor knowledge |

### Risk & Quality

| Question | Score | Evidence |
|---|---|---|
| Happy path tests? | `green` | test_engine.py, test_store.py, test_cli.py cover create→run→evaluate→decide |
| Retry and failure tests? | `green` | test_engine.py tests retry exhaustion, failure decisions, missing artifacts |
| External integration tests? | `green` | test_cli.py mocks GitHub/GitLab CLIs; test_llm_e2e.py tests LLM worker paths |
| Idempotent import tests? | `green` | test_cli.py verifies duplicate issue import returns same task |
| Actionable error capture? | `green` | Structured JSONL logging; WorkerExecutionError; LLMExecutionError |
| Event history for diagnosis? | `green` | Events track task_created, run_started, run_completed, branch_started, branch_winner_selected, etc. |
| Schema migration strategy? | `green` | migrations.py with versioned Migration records; auto-applied on init |
| CLI versioning strategy? | `yellow` | No explicit versioning yet; argparse-based with backward-compatible defaults |
| Evaluation logic evolution? | `yellow` | Validation profiles (generic, javascript, terraform) are extensible but not versioned |
| Top 3 shortcuts? | — | (1) SQLite instead of PostgreSQL, (2) synchronous local execution as primary path, (3) no auth/multi-tenancy |
| Production risks? | — | No persistent worker processes; Temporal integration untested in CI; no deployment automation |

### Product Readiness

| Question | Score | Evidence |
|---|---|---|
| Multiple projects? | `green` | project_id on all tasks; per-project concurrency limits; per-project queue filtering |
| Parallel work? | `green` | Task leasing with concurrency limits; speculative branching with winner selection (Phase 6) |
| External vs internal identity? | `green` | external_ref_type + external_ref_id separate from internal task.id |
| Report to external systems? | `green` | report-github, report-gitlab commands with optional close |
| Productivity/throughput metrics? | `green` | ops-report command with profile-aware promotion metrics |
| Follow-on task generation? | `green` | parent_task_id + source_run_id lineage; create_follow_on_task; lineage-report |
| Promotion by evaluation? | `green` | PromotionService with validator registry; affirm-promotion with LLM review |
| Reject incomplete outputs? | `green` | Required artifacts check; validation profile checks; completeness scoring |
| Long-running workflows? | `yellow` | Temporal integration exists but not battle-tested; local path is synchronous |
| Next scaling bottleneck? | — | Moving from SQLite to PostgreSQL for true concurrent multi-worker access |

### Phase Completion Status

| Phase | Status | Key Evidence |
|---|---|---|
| Phase 0: Foundation | `green` | Domain model, control loop, local tests |
| Phase 1: Durable Local Harness | `green` | Migrations, config, structured logging, smoke-test |
| Phase 2: Real Workflow Runtime | `green` | Temporal integration, runtime abstraction, run-temporal-worker |
| Phase 3: Real Worker Abstractions | `green` | WorkerBackend protocol, LLMWorker, ProfileAwareWorker, adapter registry, bounded execution |
| Phase 4: Evaluation And Promotion | `green` | PromotionService, validation profiles, follow-on tasks, rereview flow |
| Phase 5: GitLab Workflow Integration | `green` | GitLab + GitHub import/sync/report/close; deduplication; idempotency tests |
| Phase 6: Parallel Execution | `green` | Queue arbitration, task leasing, concurrency limits, speculative branching, winner selection, branch disposal |
| Phase 7: Observability | `yellow` | Telemetry scaffolding exists; OpenTelemetry not yet integrated |
| Phase 8: Interrogation | `yellow` | context-packet command exists; no full observer integration |
