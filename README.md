# accruvia-harness

`accruvia-harness` is an opinionated harness for creating and managing LLM-developed software.

It runs a durable loop:

1. plan
2. work
3. analyze
4. decide
5. repeat

The harness owns execution truth. External issue systems such as GitHub and GitLab are intake and reporting surfaces, not the control plane.

## Why This Exists

This project exists because prompt-driven agent shells were not a reliable way to run long-lived software workflows.

The harness is designed around a few non-negotiable rules:

- durable state must be explicit
- artifacts, evaluations, and decisions must be first-class records
- retries and promotions must be policy-driven
- execution and interrogation must be separate concerns
- telemetry must come from structured signals, not reconstructed chat state

## What It Does

- persists `project`, `task`, `run`, `artifact`, `evaluation`, `decision`, and `event`
- supports local and Temporal-backed runtime paths
- supports GitHub and GitLab issue intake/reporting
- supports workload adapters, project adapters, and validator plugins
- supports deterministic promotion review plus LLM affirmation
- exposes a read-only interrogation layer for explanation and ops review

## Architecture

- workflow runtime: local now, Temporal-backed path available
- system of record: SQLite today
- observability: JSONL telemetry with optional OpenTelemetry export
- worker backends: `local`, `shell`, `agent`, `llm`
- LLM routing: local CLI or `accruvia-client`

Core records:

- `project`
- `task`
- `run`
- `artifact`
- `evaluation`
- `decision`

## Source Of Truth

Execution truth is internal to the harness.

- GitHub/GitLab issues are references
- the harness DB and event history are canonical
- retries, promotions, branches, and follow-on work are governed by the harness

## Workspace And Promotion Safety

The harness now treats workspace isolation and promotion strategy as explicit project policy.

- `workspace_policy` controls whether a project may run against a shared checkout
- `promotion_mode` controls how approved isolated work is delivered:
  - `direct_main`
  - `branch_only`
  - `branch_and_pr`

Default posture:

- isolated workspaces are required
- approved work is delivered on a branch and opened for review
- open PR/MR mergeability is checked sparingly, default every 8 hours

This exists for one reason: blocked or failed runs must not dirty the main checkout, and successful isolated work must
have a deliberate path back into the real repo.

When `supervise` is running, it can also perform sparse review checks for already-open PRs/MRs. These checks are not tied
to every run. They only apply to promotions that already opened review branches, and by default they happen every 8 hours.

## Quick Start

Create a virtualenv, install the package, and initialize the harness:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
make init
make test-fast
```

Create a project and run a task:

```bash
make run ARGS="create-project accruvia 'Accruvia harness work'"
make run ARGS="create-task <project_id> 'First task' 'Build the first durable loop'"
make run ARGS="run-once <task_id>"
make run ARGS="review-promotion <task_id>"
```

Create a project with explicit repo policy:

```bash
make run ARGS="create-project routellect 'Routellect autonomous work' \
  --adapter-name routellect \
  --workspace-policy isolated_required \
  --promotion-mode branch_and_pr \
  --repo-provider github \
  --repo-name accruvia/routellect \
  --base-branch main"
```

## Common Commands

Use the `Makefile` for common developer flows:

```bash
make help
make init
make test-fast
make test
make test-e2e
make temporal-up
make temporal-down
make run ARGS="status"
```

Direct CLI entrypoints also work:

```bash
PYTHONPATH=src python3 -m accruvia_harness status
PYTHONPATH=src python3 -m accruvia_harness context-packet
PYTHONPATH=src python3 -m accruvia_harness explain-system
```

## LLM Routing

The harness separates workflow control from model execution.

- local development can route to Codex or Claude Code
- CI can route to API-backed execution or `accruvia-client`
- `ACCRUVIA_LLM_BACKEND=auto` chooses an executor by environment

Example:

```bash
export ACCRUVIA_WORKER_BACKEND=llm
export ACCRUVIA_LLM_BACKEND=auto
export ACCRUVIA_LLM_CODEX_COMMAND='codex exec < "$ACCRUVIA_LLM_PROMPT_PATH" > "$ACCRUVIA_LLM_RESPONSE_PATH"'
export ACCRUVIA_LLM_ACCRUVIA_CLIENT_COMMAND='accruvia-client llm run --prompt-file "$ACCRUVIA_LLM_PROMPT_PATH" --output-file "$ACCRUVIA_LLM_RESPONSE_PATH"'
make run ARGS="process-next --worker-id worker-a --lease-seconds 300"
```

## Interrogation

The observer path is intentionally read-only.

- `context-packet`
- `summary`
- `ops-report`
- `dashboard-report`
- `heartbeat`
- `explain-system`
- `explain-task`

Explanation commands use the configured LLM executor over read-only evidence packets and do not mutate workflow state.

## Observability

Telemetry includes:

- JSONL metrics and spans under `.accruvia-harness/telemetry`
- a durable telemetry journal plus replay state for crash recovery
- timing metrics for planning, work, analysis, decision, and promotion
- optional OpenTelemetry export
- LLM cost/token/latency rollups when executors emit metadata

Optional observability extras:

```bash
pip install -e '.[observability]'
export ACCRUVIA_OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

## Extensibility

Built-in generic adapters and validators live here. Project-specific logic usually should not.

Extension points:

- `ACCRUVIA_ADAPTER_MODULES`
- `ACCRUVIA_PROJECT_ADAPTER_MODULES`
- `ACCRUVIA_VALIDATOR_MODULES`
- `ACCRUVIA_COGNITION_MODULES`

That lets a project supply its own workspace preparation, workload evidence generation, promotion checks, and heartbeat logic without editing the harness source.

## Other Docs

- [PRODUCT_PLAN.md](/home/soverton/accruvia-harness/PRODUCT_PLAN.md)
- [ENGINEERING_CHECKLIST.md](/home/soverton/accruvia-harness/ENGINEERING_CHECKLIST.md)
- [OPERATOR_RUNBOOK.md](/home/soverton/accruvia-harness/OPERATOR_RUNBOOK.md)
- [CONTRIBUTING.md](/home/soverton/accruvia-harness/CONTRIBUTING.md)
- [specs/routellect-extraction.md](/home/soverton/accruvia-harness/specs/routellect-extraction.md)
- [specs/routellect-extraction.md](/home/soverton/accruvia-harness/specs/routellect-extraction.md)
