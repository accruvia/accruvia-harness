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
- LLM routing: local CLI or other configured command executors

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
If a merge conflict is detected, the harness records it and creates a bounded remediation follow-on task instead of polling
aggressively or trying to force the merge.
When that remediation task succeeds, the harness updates the existing review branch in place rather than opening a second PR.

`sa-watch` is intentionally independent from the main `supervise` loop. It runs as a separate sidecar so it can still
intervene when the primary supervisor is wedged and kill or repair stuck pipeline work. That independence is deliberate,
but the runtime still enforces single-instance ownership so repeated supervisor restarts do not accumulate orphaned
`sa-watch` loops.

## Quick Start

Fastest local path:

```bash
./bin/accruvia-harness setup
./bin/accruvia-harness doctor
```

The repo-local launcher bootstraps `.venv` and installs the package automatically on first use.

Prototype posture:

- treat this as a local operator appliance, not a finished packaged app
- run `doctor` and `smoke-test` before enabling long-running autonomy
- prefer one-shot supervision before continuous supervision
- use `reset-local-state --yes` when local prototype state has drifted or become suspect

If you prefer to manage the environment yourself, create a virtualenv, install the package, and initialize the harness:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
make init
make test-fast
```

Pre-release import safety:

- when running Python directly from the repo, force `src` to the front of import resolution
- use `PYTHONPATH=src ...`, `./bin/pytest-src ...`, or the `make` targets in this repo
- this is a temporary guard until packaging/release flow is stable enough to remove it

Why this is explicit right now:

- generated run workspaces under `.accruvia-harness/workspace/...` can otherwise shadow repo code
- stale imports are expensive to debug and can make tests exercise the wrong source tree

Examples:

```bash
make verify-test-import-safety
make test
make test-pytest ARGS='-q tests/test_ui.py -q'
./bin/check-test-import-safety
./bin/pytest-src -q tests/test_ui.py -q
PYTHONPATH=src python3 -m pytest -q tests/test_ui.py -q
```

Run the onboarding flow once per harness home:

```bash
./bin/accruvia-harness setup
./bin/accruvia-harness doctor
./bin/accruvia-harness smoke-test
./bin/accruvia-harness config
```

`setup` persists operator settings under `.accruvia-harness/config.json` by default, so LLM executor configuration survives
new shell sessions. Environment variables still work, but they are now best treated as overrides for CI or one-off trials.

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
./bin/accruvia-harness doctor
./bin/accruvia-harness status
./bin/accruvia-harness context-packet
./bin/accruvia-harness explain-system
```

After the environment is installed, the package entrypoint also works:

```bash
accruvia-harness doctor
accruvia-harness status
accruvia-harness context-packet
accruvia-harness explain-system
```

If you are running directly from the source tree without installing the package, use the development fallback:

```bash
PYTHONPATH=src python3 -m accruvia_harness status
```

## LLM Routing

The harness separates workflow control from model execution.

- `setup` is the default operator path and persists executor settings
- `configure-llm` is the non-interactive path for scripting or explicit control
- `doctor` reports whether heartbeats and read-only explanation flows are ready
- `doctor` now reports readiness levels for inspection, task execution, heartbeats, and autonomy
- environment variables still override persisted settings when needed

Example:

```bash
./bin/accruvia-harness configure-llm \
  --backend codex \
  --codex-command 'codex exec'

./bin/accruvia-harness doctor
make run ARGS="process-next --worker-id worker-a --lease-seconds 300"
```

For ephemeral shell-local overrides:

```bash
export ACCRUVIA_LLM_BACKEND=auto
export ACCRUVIA_LLM_CODEX_COMMAND='codex exec'
```

## Prototype Recovery

If local prototype state becomes untrustworthy, reset it explicitly:

```bash
./bin/accruvia-harness reset-local-state --yes
./bin/accruvia-harness setup
./bin/accruvia-harness init-db
./bin/accruvia-harness smoke-test
```

To preserve your persisted operator config while resetting DB, logs, telemetry, and workspaces:

```bash
./bin/accruvia-harness reset-local-state --yes --keep-config
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

## Project Brain Overrides

The harness has a global default brain prompt for heartbeat and strategy work. Projects can override it through a cognition adapter.

This is the intended way to steer a project without directly babysitting the LLM CLI:

- keep the global/default brain when the standard harness decision policy is enough
- provide a project cognition adapter when you want to bias attention toward specific product concerns or constraints
- use the project brain to clarify what deserves attention, what is out of scope, and what counts as meaningful work

This is useful when you want to nudge a project in a specific direction asynchronously, without sitting in front of the model and re-explaining priorities every run.

Project cognition adapters are loaded via `ACCRUVIA_COGNITION_MODULES`. A project-specific brain can fully replace the default prompt and decision framing when that project needs stronger or different guidance.

If you change brain code or prompt files while a supervisor is already running, the running process will not see that change until it reloads. The easiest operator path is to use `nudge-project`, which records an operator note, runs a fresh heartbeat, and gracefully reloads matching supervisors for that project when needed.

Example:

```bash
make run ARGS="nudge-project <project_id> 'Pay extra attention to onboarding, DX, and telemetry gaps'"
```

## Other Docs

- [PRODUCT_PLAN.md](/home/soverton/accruvia-harness/PRODUCT_PLAN.md)
- [ENGINEERING_CHECKLIST.md](/home/soverton/accruvia-harness/ENGINEERING_CHECKLIST.md)
- [OPERATOR_RUNBOOK.md](/home/soverton/accruvia-harness/OPERATOR_RUNBOOK.md)
- [CONTRIBUTING.md](/home/soverton/accruvia-harness/CONTRIBUTING.md)
- [specs/routellect-extraction.md](/home/soverton/accruvia-harness/specs/routellect-extraction.md)
- [specs/routellect-extraction.md](/home/soverton/accruvia-harness/specs/routellect-extraction.md)
