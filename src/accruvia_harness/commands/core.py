from __future__ import annotations

from ..domain import PromotionMode, RepoProvider, WorkspacePolicy, serialize_dataclass
from .common import CLIContext, emit


def _redact_command(value: str | None) -> str | None:
    if not value:
        return None
    command = value.strip()
    if not command:
        return None
    first = command.split()[0]
    return f"{first} [REDACTED]"


def _task_scope_from_args(args) -> dict[str, object]:
    scope: dict[str, object] = {}
    if getattr(args, "allowed_paths", None):
        scope["allowed_paths"] = list(args.allowed_paths)
    if getattr(args, "forbidden_paths", None):
        scope["forbidden_paths"] = list(args.forbidden_paths)
    return scope


def handle_core_command(args, ctx: CLIContext) -> bool:
    config = ctx.config
    store = ctx.store
    engine = ctx.engine
    if args.command == "init-db":
        emit({"db": str(config.db_path), "initialized": True, "schema_version": store.schema_version(), "expected_schema_version": store.expected_schema_version()})
        return True
    if args.command == "config":
        emit({
            "db_path": str(config.db_path),
            "workspace_root": str(config.workspace_root),
            "log_path": str(config.log_path),
            "telemetry_dir": str(config.telemetry_dir),
            "default_project_name": config.default_project_name,
            "default_repo": config.default_repo,
            "runtime_backend": config.runtime_backend,
            "temporal_target": config.temporal_target,
            "temporal_namespace": config.temporal_namespace,
            "temporal_task_queue": config.temporal_task_queue,
            "worker_backend": config.worker_backend,
            "worker_command": _redact_command(config.worker_command),
            "llm_backend": config.llm_backend,
            "llm_model": config.llm_model,
            "llm_command": _redact_command(config.llm_command),
            "llm_codex_command": _redact_command(config.llm_codex_command),
            "llm_claude_command": _redact_command(config.llm_claude_command),
            "llm_accruvia_client_command": _redact_command(config.llm_accruvia_client_command),
            "env_passthrough": list(config.env_passthrough),
            "adapter_modules": list(config.adapter_modules),
            "project_adapter_modules": list(config.project_adapter_modules),
            "validator_modules": list(config.validator_modules),
            "cognition_modules": list(config.cognition_modules),
            "timeout_ema_alpha": config.timeout_ema_alpha,
            "timeout_min_seconds": config.timeout_min_seconds,
            "timeout_max_seconds": config.timeout_max_seconds,
            "timeout_multiplier": config.timeout_multiplier,
            "memory_limit_mb": config.memory_limit_mb,
            "cpu_time_limit_seconds": config.cpu_time_limit_seconds,
            "observer_webhook_url": config.observer_webhook_url,
            "default_workspace_policy": config.default_workspace_policy,
            "default_promotion_mode": config.default_promotion_mode,
            "default_repo_provider": config.default_repo_provider,
            "default_base_branch": config.default_base_branch,
            "pr_check_enabled": config.pr_check_enabled,
            "pr_check_interval_seconds": config.pr_check_interval_seconds,
        })
        return True
    if args.command == "create-project":
        emit(
            {
                "project": serialize_dataclass(
                    engine.create_project(
                        args.name,
                        args.description,
                        adapter_name=args.adapter_name,
                        workspace_policy=WorkspacePolicy(
                            args.workspace_policy or config.default_workspace_policy
                        ),
                        promotion_mode=PromotionMode(
                            args.promotion_mode or config.default_promotion_mode
                        ),
                        repo_provider=RepoProvider(args.repo_provider or config.default_repo_provider)
                        if (args.repo_provider or config.default_repo_provider)
                        else None,
                        repo_name=args.repo_name or config.default_repo,
                        base_branch=args.base_branch or config.default_base_branch,
                    )
                )
            }
        )
        return True
    if args.command == "create-task":
        required_artifacts = args.required_artifacts or ["plan", "report"]
        scope = _task_scope_from_args(args)
        task = engine.create_task_with_policy(
            project_id=args.project_id,
            title=args.title,
            objective=args.objective,
            priority=args.priority,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=args.external_ref_type,
            external_ref_id=args.external_ref_id,
            validation_profile=args.validation_profile,
            scope=scope,
            strategy=args.strategy,
            max_attempts=args.max_attempts,
            max_branches=args.max_branches,
            required_artifacts=required_artifacts,
        )
        emit({"task": serialize_dataclass(task)})
        return True
    if args.command == "run-once":
        run = engine.run_once(args.task_id)
        emit({"run": serialize_dataclass(run), "artifacts": [serialize_dataclass(i) for i in store.list_artifacts(run.id)], "evaluations": [serialize_dataclass(i) for i in store.list_evaluations(run.id)], "decisions": [serialize_dataclass(i) for i in store.list_decisions(run.id)]})
        return True
    if args.command == "run-until-stable":
        runs = engine.run_until_stable(args.task_id)
        emit({"runs": [serialize_dataclass(r) for r in runs], "task": serialize_dataclass(store.get_task(args.task_id))})
        return True
    if args.command == "process-next":
        result = engine.process_next_task(args.project_id, worker_id=args.worker_id, lease_seconds=args.lease_seconds)
        emit({"processed": None} if result is None else {"task": serialize_dataclass(result["task"]), "runs": [serialize_dataclass(r) for r in result["runs"]]})
        return True
    if args.command == "process-queue":
        results = engine.process_queue(args.limit, args.project_id, worker_id=args.worker_id, lease_seconds=args.lease_seconds)
        emit({"processed": [{"task": serialize_dataclass(i["task"]), "runs": [serialize_dataclass(r) for r in i["runs"]]} for i in results]})
        return True
    if args.command == "supervise":
        max_idle_cycles = args.max_idle_cycles
        if max_idle_cycles is None:
            max_idle_cycles = None if args.watch else 1
        result = engine.supervise(
            project_id=args.project_id,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            watch=args.watch,
            idle_sleep_seconds=args.idle_sleep_seconds,
            max_idle_cycles=max_idle_cycles,
            max_iterations=args.max_iterations,
            heartbeat_project_ids=args.heartbeat_project_ids,
            heartbeat_interval_seconds=args.heartbeat_interval_seconds,
            heartbeat_all_projects=args.heartbeat_all_projects,
            review_check_enabled=args.review_check_enabled or config.pr_check_enabled,
            review_check_interval_seconds=args.review_check_interval_seconds or config.pr_check_interval_seconds,
        )
        emit(serialize_dataclass(result))
        return True
    if args.command == "check-reviews":
        emit(
            serialize_dataclass(
                engine.check_reviews(args.interval_seconds or config.pr_check_interval_seconds)
            )
        )
        return True
    if args.command == "review-promotion":
        result = engine.review_promotion(args.task_id, run_id=args.run_id, create_follow_on=not args.no_follow_on)
        emit({"promotion": serialize_dataclass(result.promotion), "follow_on_task_id": result.follow_on_task_id})
        return True
    if args.command == "affirm-promotion":
        result = engine.affirm_promotion(
            args.task_id,
            run_id=args.run_id,
            promotion_id=args.promotion_id,
            create_follow_on=not args.no_follow_on,
        )
        emit({"promotion": serialize_dataclass(result.promotion), "follow_on_task_id": result.follow_on_task_id})
        return True
    if args.command == "rereview-promotion":
        result = engine.rereview_promotion(
            args.task_id,
            remediation_task_id=args.remediation_task_id,
            remediation_run_id=args.remediation_run_id,
            base_promotion_id=args.base_promotion_id,
            create_follow_on=not args.no_follow_on,
        )
        emit({"promotion": serialize_dataclass(result.promotion), "follow_on_task_id": result.follow_on_task_id})
        return True
    if args.command == "smoke-test":
        project = engine.create_project(args.project_name, "Local smoke-test project", adapter_name="generic")
        task = engine.create_task_with_policy(
            project_id=project.id, title=args.task_title, objective=args.objective,
            priority=100, parent_task_id=None, source_run_id=None,
            external_ref_type=None, external_ref_id=None,
            validation_profile="generic", strategy="smoke", max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        runs = engine.run_until_stable(task.id)
        emit({"project": serialize_dataclass(project), "task": serialize_dataclass(store.get_task(task.id)), "runs": [serialize_dataclass(r) for r in runs], "events": [serialize_dataclass(i) for i in store.list_events("task", task.id)]})
        return True
    return False
