# Contributing

## Local Setup

```bash
make init
```

Optional extras:

```bash
make install-temporal
make install-observability
```

## Test Layout

The suite is intentionally split by cost:

- `make test-fast`
  - core logic, storage, validation, interrogation, observer, parallelism
- `make test-e2e`
  - CLI integration
  - routed LLM integration
  - Temporal end-to-end
- `make test-temporal`
  - required gate for Temporal/runtime changes
  - starts local Temporal, runs runtime + Temporal E2E coverage, and tears the stack down
- `make test`
  - full suite

Current timing profile is roughly:

- `tests.test_cli`: the slowest module
- `tests.test_temporal_e2e`: moderate
- most unit-style modules: fast

So if you are iterating on core logic, start with `make test-fast`. Use `make test-e2e` when changing CLI or LLM routing. Use `make test-temporal` for any change touching `runtime.py`, `temporal_backend.py`, `bootstrap.py`, `config.py`, or run-loop semantics.

## Development Guidelines

- keep workflow truth in the harness, not in external issue systems
- keep interrogation paths read-only
- prefer extension points over project-specific logic in this repo
- add tests for any new runtime, promotion, or observer behavior
- avoid leaking ambient environment into subprocess execution

## Common Flows

Run the CLI:

```bash
make run ARGS="status"
```

Start Temporal locally:

```bash
make temporal-up
make run ARGS="run-temporal-worker"
```

Stop Temporal:

```bash
make temporal-down
```
