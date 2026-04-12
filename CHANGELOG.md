# Changelog

## 2026-04-11 — Add enqueue_decision and dequeue_decision to store

Added DecisionQueueItem dataclass to domain.py, a decision_queue_item_from_row helper to common.py, and three queue methods (enqueue_decision, dequeue_decision, complete_decision) to RunRecordsStoreMixin. The dequeue method atomically selects the oldest pending item by priority then created_at and updates it to 'processing' within one connection context. Tests cover FIFO ordering, priority ordering, completion status transitions, and idempotent dequeue-when-empty.

**Files changed:** src/accruvia_harness/domain.py, src/accruvia_harness/persistence/common.py, src/accruvia_harness/persistence/run_records.py, tests/test_store.py

## 2026-04-11 — Add decision_queue table migration to store.py

Added Migration 18 (decision_queue) to the MIGRATIONS list in migrations.py, following the exact pattern of Migration 17 (validation_queue) but with evaluation_id instead of snapshot_id. Added a companion test in test_store.py that queries sqlite_master to confirm the table exists after initialize().

**Files changed:** src/accruvia_harness/migrations.py, tests/test_store.py

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

