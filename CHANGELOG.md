# Changelog

## 2026-04-11 — Route UIDataService.add_operator_comment through ContextRecorder.record_operator_comment

Created ContextRecorder in a new module to encapsulate the operator-comment ContextRecord creation and store persistence. Refactored add_operator_comment to delegate the initial mutation to ContextRecorder.record_operator_comment while preserving all downstream behavior (frustration detection, task-question enqueueing, mermaid proposals, reply creation). Added a dedicated test file verifying the recorder path, responder integration, and frustration detection.

**Files changed:** src/accruvia_harness/ui.py, src/accruvia_harness/context_recorder.py, tests/test_ui_add_operator_comment_recorder.py

## 2026-04-11 — Add validation_queue table migration to store.py

Added migration version 17 creating the validation_queue table with the specified columns (id, run_id, task_id, snapshot_id, priority, created_at, status with default 'pending', started_at, completed_at). Added a test that initializes the store and asserts the table exists via sqlite_master.

**Files changed:** src/accruvia_harness/migrations.py, tests/test_store.py

## 2026-04-11 — BREAKING: remove agent worker backend, skills only

Pre-alpha hard cutover. Skills is now the only supported worker backend. The
agent worker, shell worker, command worker, and LLM task worker have all been
deleted with no migration path or deprecation cycle. Any persisted config that
still carries `worker_backend` or `worker_command` will fail to load with a
"removed in pre-alpha" error; edit `.accruvia-harness/config.json` to drop
those keys.

## 2026-04-06 — Add pre-flight budget check before starting tasks

Added a pre-flight budget check at the top of SkillsWorkOrchestrator.execute(). Before any LLM skill invocations, it instantiates CostTracker and calls check_budget(task.project_id) with the built-in 20.0 USD default. If over budget and the task's validation_mode is not 'lightweight_operator', it returns early with outcome='blocked' and the specified diagnostics. Lightweight operator tasks bypass the check since they use no LLM calls.

**Files changed:** src/accruvia_harness/services/work_orchestrator.py

All notable changes to this project.

