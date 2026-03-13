# Atomicity Telemetry

## Purpose

`Atomicity Telemetry` is the attempt-level data product that supports atomicity gating, later weight tuning, and honest analysis of why self-hosted and user-project tasks succeed or fail.

The goal is to record enough structured evidence about each attempt that the harness can:

- make deterministic pre-validation gating decisions
- audit why a gate decision was made
- learn better thresholds or weights later
- avoid losing potentially useful features that cannot be reconstructed after the fact

## Design Principles

- collect raw features, not just derived scores
- keep features deterministic and reproducible
- version the schema explicitly
- distinguish `0`, `false`, and `unknown`
- record lineage for every attempt
- prefer structured counts and flags over opaque summaries
- capture features before expensive deterministic validation begins

## Collection Point

The primary collection point is:

1. candidate generation has completed
2. changed-file inventory is available
3. immutable attempt snapshot exists or the equivalent pre-validation artifact set exists
4. compile/test validation has not yet started

This is the point where the system can still choose whether to validate normally, validate narrowly, decompose, or block.

## Entity Scope

Telemetry is recorded per attempt.

Minimum identifying fields:

- `schema_version`
- `project_id`
- `task_id`
- `run_id`
- `attempt`
- `parent_task_id`
- `source_run_id`
- `project_name`
- `task_strategy`
- `validation_profile`
- `validation_mode`
- `timestamp`

## Unknown And Unavailable Values

Feature values must preserve missingness explicitly.

Rules:

- use `null` for unavailable or not-yet-known values
- use `0` only when the measured quantity is known to be zero
- use `false` only when a boolean check was performed and the answer is negative
- include a `feature_status` map for features that are skipped, unavailable, or intentionally deferred

Example:

```json
{
  "functions_added": null,
  "feature_status": {
    "functions_added": "ast_parse_failed"
  }
}
```

## Feature Groups

### 1. Diff Size

- `changed_file_count`
- `added_file_count`
- `deleted_file_count`
- `renamed_file_count`
- `changed_hunk_count`
- `lines_added`
- `lines_deleted`
- `lines_changed_total`
- `net_line_delta`
- `characters_added`
- `characters_deleted`
- `characters_changed_total`
- `max_lines_changed_in_single_file`
- `median_lines_changed_per_file`

### 2. Code Shape

- `functions_added`
- `functions_removed`
- `functions_modified`
- `methods_added`
- `methods_removed`
- `methods_modified`
- `classes_added`
- `classes_removed`
- `classes_modified`
- `imports_added`
- `imports_removed`
- `signature_changes`
- `public_symbol_changes`
- `field_or_attribute_changes`

### 3. Surface Area

- `subsystem_count`
- `top_level_directory_count`
- `touches_runtime_code`
- `touches_test_code`
- `touches_docs`
- `touches_config`
- `touches_ci`
- `touches_migrations`
- `touches_persistence`
- `touches_policy`
- `touches_worker_runtime`
- `touches_cli_surface`
- `touches_observer_surface`
- `touches_integration_surface`

### 4. Control Plane And Self-Reference

- `project_is_self_hosting`
- `touches_control_plane`
- `touches_validation_policy`
- `touches_retry_logic`
- `touches_supervisor_semantics`
- `touches_task_selection`
- `touches_cognition_or_heartbeat`
- `touches_current_validation_class_definition`
- `self_referential_change_detected`

### 5. Task And Intent Alignment

- `objective_token_count`
- `title_token_count`
- `objective_keyword_path_overlap_count`
- `objective_keyword_symbol_overlap_count`
- `files_outside_allowed_paths_count`
- `files_inside_forbidden_paths_count`
- `operator_task_touches_non_operator_surface`
- `policy_task_touches_feature_surface`
- `intent_surface_mismatch_detected`

### 6. Validation Scope

- `selected_validation_target_count`
- `selected_validation_module_count`
- `model_proposed_validation_target_count`
- `touched_test_file_count`
- `touched_non_test_file_count`
- `touched_files_without_validation_target_count`
- `validation_surface_breadth`
- `compile_target_count`
- `estimated_validation_blast_radius`

### 7. Retry And History

- `retry_attempt_number`
- `prior_failed_run_count`
- `prior_blocked_run_count`
- `prior_timeout_count`
- `prior_validation_timeout_count`
- `prior_validation_startup_timeout_count`
- `prior_decomposition_count`
- `recent_project_retry_waste`
- `recent_same_strategy_failure_count`
- `recent_same_validation_mode_failure_count`
- `recent_same_surface_failure_count`

### 8. Execution Dynamics

- `llm_generation_duration_ms`
- `time_to_first_artifact_ms`
- `time_to_compile_artifact_ms`
- `time_to_test_artifact_ms`
- `time_to_first_failing_assertion_ms`
- `progress_event_count`
- `stale_progress_timeout_triggered`
- `validation_startup_timeout_triggered`
- `validation_execution_timeout_triggered`

### 9. Artifact Quality

- `plan_length_chars`
- `report_length_chars`
- `summary_length_chars`
- `report_changed_files_match_diff`
- `report_test_files_match_diff`
- `report_compile_targets_match_diff`
- `artifact_consistency_error_count`

### 10. Repository Context

- `repo_python_file_count`
- `repo_test_file_count`
- `repo_module_count`
- `dirty_worktree_file_count_at_start`
- `snapshot_size_bytes`

## File Surface Classification

The telemetry pipeline should record the set of touched surface classes, not just raw paths.

Examples:

- `cli_surface`
- `control_plane`
- `validation_policy`
- `worker_runtime`
- `task_selection`
- `supervisor_semantics`
- `observer_surface`
- `persistence_layer`
- `test_only`

These categories support both deterministic gating and later analysis.

## Outcome Labels

Telemetry should eventually be joined with run outcomes and validation outcomes.

Minimum labels:

- `run_status`
- `worker_outcome`
- `evaluation_verdict`
- `decision_action`
- `failure_category`
- `gate_action`
- `task_terminal_status`
- `promotion_status`

Useful fine-grained labels:

- `assertion_failure`
- `validation_timeout`
- `validation_startup_timeout`
- `validation_scope_mismatch`
- `policy_self_modification`
- `executor_process_failure`
- `decomposed_before_validation`

## Persistence

Telemetry should be persisted as structured records, not just log lines.

Acceptable first implementations:

- JSONL journal with schema version
- SQLite table keyed by `run_id`
- both, if the journal is the append-only source and SQLite is the queryable cache

## Derived Fields

Derived fields are allowed, but raw inputs must still be kept.

Examples of derived fields:

- `atomicity_risk_score`
- `surface_mix_signature`
- `validation_scope_ratio`
- `self_hosting_control_plane_risk`

## First Consumers

The first consumer is the `Atomicity Gate`.

Later consumers may include:

- heuristic tuning
- supervised or semi-supervised weighting
- evolutionary / genetic threshold tuning
- dashboards
- operator reports

## Non-Goals

This telemetry spec does not define:

- the gate policy itself
- queue semantics
- learned tuning strategy

Those belong in separate specs.
