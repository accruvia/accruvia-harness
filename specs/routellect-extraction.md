# Routellect Extraction Map

`Routellect` should be the standalone routing and issue-runner repo extracted from private Accruvia code. It should not absorb Accruvia business logic or the harness control plane.

## Product Boundary

`Routellect` owns:

- model recommendation and routing contracts
- optional remote routing client
- issue-runner execution flow
- QA review flow around generated artifacts
- run-level telemetry, token usage, and cost reporting
- issue intake abstractions only if the router itself needs to ingest issues directly

`accruvia-harness` owns:

- tasks, runs, evaluations, decisions, promotions, and branches
- retry and promotion policy
- durable workflow execution
- observability over workflow state
- issue tracker sync/reporting as control-plane behavior

## Recommended Routellect Repo Structure

```text
routellect/
  README.md
  pyproject.toml
  src/
    routellect/
      __init__.py
      __main__.py
      runner.py
      qa_panel.py
      protocols.py
      server_client.py
      identity.py
      issue_fetcher.py          # optional
      decisions.py              # optional
      report.py                 # optional
      telemetry/
        __init__.py
        cost_model.py
        run_logger.py
  tests/
    test_runner.py
    test_qa_panel.py
    test_server_client.py
    test_identity.py
    test_cost_model.py
    test_run_logger.py
    test_issue_fetcher.py       # optional
    test_decisions.py           # optional
```

## Move

These are strong candidates to move into `Routellect` with package renaming:

- `src/accruvia_client/runner.py`
- `src/accruvia_client/qa_panel.py`
- `src/accruvia_client/protocols.py`
- `src/accruvia_client/server_client.py`
- `src/accruvia_client/identity.py`
- `src/accruvia_client/telemetry/cost_model.py`
- `src/accruvia_client/telemetry/run_logger.py`

These should move only if `Routellect` explicitly owns the feature:

- `src/accruvia_client/issue_fetcher.py`
  - keep if `Routellect` will fetch GitHub/GitLab/local issues itself
  - leave behind if issue intake belongs to `accruvia-harness`
- `src/accruvia_client/decisions.py`
  - keep if A/B winner/retirement tracking is part of the router product
- `src/accruvia_client/report.py`
  - keep if run comparison/reporting belongs in the router repo

## Do Not Move

These stay out of `Routellect`:

- `src/accruvia/orchestration/**`
- harness-specific or repo-specific control-plane logic
- business-domain code from `accruvia/**`
- scripts that exist only to operate the private monorepo

`Routellect` should not become the new home for private orchestration logic.

## Renaming Guidance

Recommended package rename:

- `accruvia_client` -> `routellect`

Recommended CLI shape:

- `routellect run-issue ...`
- `routellect route ...`
- `routellect report ...`

Avoid carrying private `accruvia` references in public module names unless they represent a real public API contract.

## Harness Integration

The clean relationship is:

- `accruvia-harness` = workflow controller
- `Routellect` = routing and issue-runner execution component

In practice:

1. The harness chooses work and prepares a workspace.
2. The harness calls a worker backend.
3. The worker backend invokes `Routellect` locally or through an API/client surface.
4. `Routellect` runs the issue-level execution loop and emits structured artifacts.
5. The harness evaluates those artifacts, decides promotion/retry/branch/follow-on, and records workflow truth.

## Current Heartbeat Pattern

The harness now supports a separate cognition adapter surface for project heartbeats.

For `Routellect`, the intended pattern is:

1. load `routellect.harness_plugins` through:
   - `ACCRUVIA_PROJECT_ADAPTER_MODULES`
   - `ACCRUVIA_COGNITION_MODULES`
2. create the project with `adapter_name=\"routellect\"`
3. run:

```bash
PYTHONPATH=/home/soverton/routellect/src:/home/soverton/accruvia-harness/src \
ACCRUVIA_PROJECT_ADAPTER_MODULES=routellect.harness_plugins \
ACCRUVIA_COGNITION_MODULES=routellect.harness_plugins \
ROUTELLECT_REPO_ROOT=/home/soverton/routellect \
python3 -m accruvia_harness heartbeat <project_id>
```

That keeps project-specific strategic reasoning in the project repo instead of hardcoding it into harness core.

## Integration Modes

### Mode 1: Local CLI

The harness runs `Routellect` as a local command.

Use when:

- local development
- a single machine trial
- operator-driven workflows

Example:

```bash
export ACCRUVIA_WORKER_BACKEND=llm
export ACCRUVIA_LLM_BACKEND=command
export ACCRUVIA_LLM_COMMAND='routellect run-issue --prompt-file "$ACCRUVIA_LLM_PROMPT_PATH" --output-file "$ACCRUVIA_LLM_RESPONSE_PATH"'
```

### Mode 2: API-backed routing

The harness uses `Routellect` through a service client.

Use when:

- CI
- shared routing policy
- central model/budget/fallback control

This is where the existing `server_client.py` concept fits.

### Mode 3: Embedded library

The harness imports `Routellect` directly through an adapter module.

Use when:

- the deployment is fully private
- you want fewer process boundaries
- you control both repos tightly

This is viable, but local CLI or API boundaries are usually cleaner for early trials.

## Recommended First Extraction

For the first repo cut:

1. move the core package files listed in `Move`
2. bring only the focused tests that cover them
3. leave `issue_fetcher.py`, `decisions.py`, and `report.py` behind unless you know they are product features
4. keep the harness integration initially at the CLI boundary

That gives you a smaller, clearer `Routellect` repo and avoids copying private orchestration assumptions into the public split.
