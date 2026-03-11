from __future__ import annotations

from ..domain import serialize_dataclass
from .common import CLIContext, emit


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
            "worker_command": config.worker_command,
            "llm_backend": config.llm_backend,
            "llm_model": config.llm_model,
            "llm_command": config.llm_command,
            "llm_codex_command": config.llm_codex_command,
            "llm_claude_command": config.llm_claude_command,
            "llm_accruvia_client_command": config.llm_accruvia_client_command,
            "adapter_modules": list(config.adapter_modules),
            "project_adapter_modules": list(config.project_adapter_modules),
            "timeout_ema_alpha": config.timeout_ema_alpha,
            "timeout_min_seconds": config.timeout_min_seconds,
            "timeout_max_seconds": config.timeout_max_seconds,
            "timeout_multiplier": config.timeout_multiplier,
            "memory_limit_mb": config.memory_limit_mb,
            "cpu_time_limit_seconds": config.cpu_time_limit_seconds,
        })
        return True
    if args.command == "create-project":
        emit({"project": serialize_dataclass(engine.create_project(args.name, args.description, adapter_name=args.adapter_name))})
        return True
    if args.command == "create-task":
        required_artifacts = args.required_artifacts or ["plan", "report"]
        task = engine.create_task_with_policy(
            args.project_id,
            args.title,
            args.objective,
            args.priority,
            None,
            None,
            args.external_ref_type,
            args.external_ref_id,
            args.validation_profile,
            args.strategy,
            args.max_attempts,
            required_artifacts,
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
    if args.command == "smoke-test":
        project = engine.create_project(args.project_name, "Local smoke-test project", adapter_name="generic")
        task = engine.create_task_with_policy(
            project.id, args.task_title, args.objective, 100, None, None, None, None, "generic", "smoke", 2, ["plan", "report"]
        )
        runs = engine.run_until_stable(task.id)
        emit({"project": serialize_dataclass(project), "task": serialize_dataclass(store.get_task(task.id)), "runs": [serialize_dataclass(r) for r in runs], "events": [serialize_dataclass(i) for i in store.list_events("task", task.id)]})
        return True
    return False
