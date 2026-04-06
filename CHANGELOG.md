# Changelog

## 2026-04-06 — Add pre-flight budget check before starting tasks

Added a pre-flight budget check at the top of SkillsWorkOrchestrator.execute(). Before any LLM skill invocations, it instantiates CostTracker and calls check_budget(task.project_id) with the built-in 20.0 USD default. If over budget and the task's validation_mode is not 'lightweight_operator', it returns early with outcome='blocked' and the specified diagnostics. Lightweight operator tasks bypass the check since they use no LLM calls.

**Files changed:** src/accruvia_harness/services/work_orchestrator.py

All notable changes to this project.

