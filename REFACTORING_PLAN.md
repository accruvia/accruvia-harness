# Accruvia Harness Refactoring Plan

**Last updated:** 2026-04-18
**Tests:** 926 passing, 6 skipped

## What's been done

### ui.py monolith decomposition (COMPLETE)
- **6,937 → 450 lines** base class
- 11 domain mixins in `ui_mixins/`
- Routes in `ui_routes.py`, coordinators in `ui_coordinators.py`
- Shared constants/helpers in `ui_mixins/_shared.py`

### Objective review decomposition (COMPLETE)
- **2,002 → 832 lines** in the mixin
- 7 focused service classes:
  - `ReviewRound` (domain.py) — packet list, review_clear, verdict_counts
  - `ReviewPacketRepository` (services/) — save/load packets from ContextRecords
  - `RemediationService` (services/) — create remediation tasks, evidence contracts
  - `ReviewPromptBuilder` (services/) — prompt building, response parsing, packet validation
  - `ReviewStateService` (services/) — review state queries, staleness, usage tracking
  - `ReviewCycleRecorder` (services/) — cycle artifacts, worker responses, rebuttals
  - `PromotionReviewBuilder` (services/) — 369-line view builder for promotion status

### Typed domain classes (COMPLETE)
- 6 enums: `ReviewVerdict`, `ReviewSeverity`, `ReviewProgressStatus`, `ReviewDimension`, `PlanComplexity`, `OrphanStrategy`
- 5 dataclasses with `from_dict()`/`to_dict()`: `ReviewPacket`, `PlanSlice`, `InterrogationReview`, `ArtifactSchema`, `EvidenceContract`
- `ReviewRound` domain class
- All construction sites use typed classes; consumers still use `.get()` dict access (safe — `to_dict()` guarantees shape)

### Pipeline architecture (COMPLETE)
- TRIO replaces old atomic decomposition: `interrogation → mermaid → TRIO → tasks`
- Scope LLM call eliminated for TRIO tasks (scope derived from plan)
- `ObjectiveLifecycleWorkflow` (Temporal) + `ObjectiveLifecycleRunner` (local) enforce phase sequence
- `ObjectivePhase` enum with `advance_objective_phase()` — single enforcement point
- Phase persisted in DB (migration 20)
- Implement+self_review retry loop (max 2 rounds)
- `trio_plan_orchestrator` bundles plan_draft_trio + review_plan_atomicity in red-team loop

---

## What's left

### 1. commands/core.py — 1,666 lines (HIGH priority)

**Problem:** Every CLI command handler in one file. Mixes objective commands, task commands, supervisor commands, review commands, and utility commands.

**Fix:** Split by domain into `commands/objectives.py`, `commands/tasks.py`, `commands/supervisor.py`, `commands/review.py`. Keep `core.py` as the click group entry point that registers subcommands.

**Pattern:** Same as ui.py mixin decomposition — cut methods, paste into domain files, keep thin delegates.

**Risk:** Low. CLI commands are self-contained functions decorated with `@click.command`. No cross-references between commands.

### 2. sa_watch.py — 1,601 lines (HIGH priority)

**Problem:** SA Watch is likely a god class mixing:
- File/process monitoring
- Triage logic (classifying alerts)
- Structural fix generation
- Alert recording

**Fix:** Investigate first — `grep -n "class\|def " sa_watch.py | head -40` to understand structure. Then extract:
- `SAWatchMonitor` — watches for changes
- `SAWatchTriageService` — classifies and routes alerts
- `StructuralFixService` — generates fixes from alerts
- Keep `sa_watch.py` as thin orchestration

**Risk:** Medium. Need to understand the class structure before planning extraction.

### 3. work_orchestrator.py — 1,346 lines (MEDIUM priority)

**Problem:** The 8-stage skills pipeline (`scope → implement → self_review → validate → diagnose → fix_tests → quality_gate → commit`) in one `_execute` method (~800 lines). Each stage is a sequential block with error handling.

**Fix:** Extract each stage into a `StageHandler` class:
```python
class ImplementStage:
    def execute(self, context: PipelineContext) -> StageResult: ...

class SelfReviewStage:
    def execute(self, context: PipelineContext) -> StageResult: ...
```

Then `_execute` becomes a loop over stage handlers. The implement+self_review retry loop lives in a `RetryableStageGroup`.

**Risk:** Medium. The stages share state (scope output feeds implement, implement feeds self_review). Need a `PipelineContext` dataclass to carry state between stages.

### 4. run_service.py — 1,106 lines (MEDIUM priority)

**Problem:** Mixes two distinct phases (`_work_phase` and `_validation_phase`) with retry logic, diagnostic recording, and auto-merge (hobbled).

**Fix:**
- Extract `WorkPhase` class — owns workspace prep, worker execution, result collection
- Extract `ValidationPhase` class — owns analysis, decision, status application
- Extract `RetryAdvisor` (may already exist) — owns retry hint extraction
- `RunService` becomes orchestration: create run → work phase → validation phase → apply status

**Risk:** Low-medium. The two phases are already separate methods. The challenge is shared state (run, task, workspace).

### 5. commands/common.py — 1,084 lines (LOW priority)

**Problem:** Grab bag of CLI utilities. Likely contains: project resolution, config loading, output formatting, runtime state management, plus helpers that should be in services.

**Fix:** Audit what's in it (`grep -n "^def \|^class " commands/common.py`), then:
- Move business logic to services
- Move formatting to `commands/formatting.py`
- Move config/runtime state to `commands/config.py`
- Keep `common.py` for genuinely shared CLI primitives (< 200 lines)

**Risk:** Low. These are utility functions, not deeply coupled.

---

## Files that are fine as-is

These are large but focused — each file has one job:

| File | Lines | Why it's OK |
|---|---|---|
| domain.py | 917 | Typed dataclasses + enums. Growing is expected. |
| interrogation.py | 839 | Single service. May duplicate InterrogationMixin — audit for overlap. |
| plan_draft.py | 721 | Skill + TRIO variant + materialization. Focused. |
| chaos/injectors.py | 703 | Chaos testing. Self-contained. |
| promotion_service.py | 657 | One of 4 promotion services — consolidation opportunity but not urgent. |
| llm.py | 637 | LLM routing + executors. Focused. |

---

## How to execute each refactor

Each refactor follows the same pattern we used for ui.py:

1. **Map methods** — `python3 -c "import ast; ..."` to get method names + line ranges
2. **Group by domain** — identify which methods belong together based on call graph
3. **Extract leaf groups first** — groups with no outbound dependencies
4. **Create service class** — copy methods, add constructor with dependencies
5. **Fix imports** — relative imports change from `.` to `..` when moving to services/
6. **Add delegate** — thin wrapper in original file for backward compat
7. **Test** — `pytest tests/test_ui.py -x -q` after each extraction
8. **Full suite** — `pytest tests/ -q` after each batch

**Critical rule:** Never extract a method without running tests immediately after. One method at a time, tests between each.

---

## Verification

After any refactor session:
```bash
python3 -m pytest tests/ -q                    # all 926 tests pass
wc -l src/accruvia_harness/**/*.py | sort -rn   # no file over 1000 lines (goal)
grep -rn "def " <file> | wc -l                 # method count sanity check
```
