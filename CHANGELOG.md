# Changelog

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

