# Skills Migration Plan

Replace the `working` and `promoting` workflows with a gstack-inspired skills model
while preserving the discipline that makes the `planning` workflow work.

## Core Insight

Planning treats the LLM as a **narrow role with schema-bounded output**.
Working and promoting treat the LLM as a **monolithic agent** and try to interpret
its behavior post-hoc. The fix is to extend planning's pattern across the whole loop.

| | Planning (good) | Working + Promoting (bad) |
|---|---|---|
| LLM role | Narrow role, schema output | Monolithic agent |
| Prompt ownership | Harness owns the prompt | External CLI owns the prompt |
| Output contract | Strict JSON, validated on materialization | Free-form `report.json`, brittle |
| Failure handling | Invalid entries skipped gracefully | Missing field = whole run fails |
| Control flow | Deterministic Python composes LLM output | Tangled Python interprets opaque output |

## What to Keep (from planning)

1. Declarative prompts that name evaluation dimensions explicitly
2. Schema-enforced structured outputs
3. Deterministic materialization layer (validates, dedupes, skips invalid)
4. Bounded retry context (small, precise)
5. Idempotent by design

## What to Throw Away

- `src/accruvia_harness/workers.py` — external worker CLI abstraction
- `src/accruvia_harness/agent_worker.py` — report.json validation contract
- `src/accruvia_harness/control_classifier.py` — regex keyword failure classifier
- `src/accruvia_harness/frustration_triage.py` — heuristic triage
- Monolithic affirmation prompt in `services/promotion_service.py`
- Scattered retry logic across `DefaultDecider`, `RetryStrategyAdvisor`,
  `FailureClassifier` (they don't coordinate)
- The work + validation phases in `services/run_service.py` (rewritten around skills)

## Skill Contract

Every skill has the same shape (mirrors `CognitionService.heartbeat()`):

```
skill(inputs) -> structured_json (schema-enforced)
              -> materialize_deterministically(json) -> DB records / artifacts / events
```

Skill = `{prompt_template, output_schema, materialize_fn}`.
Backend = single `llm_router.call_with_schema(prompt, schema)`.
No subprocesses, no env-var contracts, no `report.json`.

## New Skill Pipeline — Working

| Skill | Role | Input | Structured Output |
|---|---|---|---|
| `/scope` | Tech lead | Task + repo context | `{files_to_touch[], files_not_to_touch[], risks[], approach}` |
| `/implement` | Engineer | Scope + task | Code edits + `{changed_files[], rationale}` |
| `/self-review` | Staff engineer | Diff | `{issues[], ship_ready: bool}` |
| `/validate` | QA (deterministic, not LLM) | Diff + repo | `{compile: pass/fail, tests: pass/fail, evidence_path}` |
| `/diagnose` | Debugger | Failed validation output | `{root_cause, class, retry_recommended, scope_adjustment}` |

`/diagnose` replaces `control_classifier.py` and `frustration_triage.py`.

## New Skill Pipeline — Promoting

| Skill | Role | Input | Structured Output |
|---|---|---|---|
| `/promotion-review` | Reviewer | Diff + task objective | `{concerns[], approved: bool, rationale}` |
| `/promotion-apply` | Release engineer (control plane) | Approved review | Git ops |
| `/post-merge-check` | SRE | Merge commit | `{main_healthy: bool, rollback_needed: bool}` |
| `/follow-on` | PM | Rejected review | Task proposal using cognition's existing schema |

`/follow-on` emits the same `proposed_tasks[]` schema as `CognitionService`, so the
existing materialization layer handles it.

## State Persistence

Keep the DB as handoff medium (better than gstack's Markdown files — gives durability,
replay, telemetry). Store each skill output as structured JSON in the artifacts table
so the next skill can read it directly.

## Control Plane

The control plane becomes a deterministic state machine over structured skill outputs
instead of an interpreter of opaque worker behavior. Dispatch is `match skill_output.action`.

## Migration Order

1. Build the skill framework — one unified `Skill` abstraction with `{prompt_template, output_schema, materialize_fn}` and an `llm_router.call_with_schema()` backend
2. Build `/diagnose` as first skill end-to-end (narrow, self-contained, proves the pattern)
3. Delete `control_classifier.py`, `frustration_triage.py`; wire `/diagnose` into control plane
4. Build the four work skills (`/scope`, `/implement`, `/self-review`, `/validate`)
5. Rewrite work phase in `run_service.py` as orchestration over skills
6. Delete `workers.py`, `agent_worker.py`, and the report.json contract
7. Build the four promotion skills
8. Rewrite `promotion_service.py` as orchestration over skills
9. Move `/promotion-apply` ownership to control plane (fixes CONTROL-PLANE-PLAN.md:56-58 gap)
10. Add post-merge validation (closes CONTROL-PLANE-PLAN.md:82-83 gap)
11. Update tests
