from __future__ import annotations

from dataclasses import asdict

from ..domain import serialize_dataclass
from .common import CLIContext, emit, ensure_llm_ready


def handle_interrogation_command(args, ctx: CLIContext) -> bool:
    store = ctx.store
    if args.command == "status":
        emit({"projects": [serialize_dataclass(i) for i in store.list_projects()], "tasks": [serialize_dataclass(i) for i in store.list_tasks()], "runs": [serialize_dataclass(i) for i in store.list_runs()], "promotions": [serialize_dataclass(i) for i in store.list_promotions()], "leases": [serialize_dataclass(i) for i in store.list_task_leases()], "schema_version": store.schema_version()})
        return True
    if args.command == "summary":
        emit(ctx.query_service.project_summary(args.project_id) if args.project_id else ctx.query_service.portfolio_summary())
        return True
    if args.command == "context-packet":
        emit(ctx.query_service.context_packet(args.project_id))
        return True
    if args.command == "ops-report":
        payload = ctx.query_service.operations_report(args.project_id)
        payload["telemetry"] = ctx.telemetry.summary()
        emit(payload)
        return True
    if args.command == "telemetry-report":
        emit(ctx.telemetry.summary())
        return True
    if args.command == "dashboard-report":
        emit(ctx.query_service.dashboard_report(args.project_id))
        return True
    if args.command == "heartbeat":
        ensure_llm_ready(args, ctx, reason="Project heartbeat")
        heartbeat = ctx.engine.heartbeat(args.project_id)
        payload = {"heartbeat": asdict(heartbeat)}
        if heartbeat.created_tasks and not args.no_process_created_tasks:
            payload["processing"] = asdict(
                ctx.engine.supervise(
                    project_id=args.project_id,
                    worker_id=args.worker_id,
                    lease_seconds=args.lease_seconds,
                    watch=False,
                    max_idle_cycles=1,
                    heartbeat_project_ids=None,
                    heartbeat_interval_seconds=None,
                )
            )
        emit(payload)
        return True
    if args.command == "explain-system":
        ensure_llm_ready(args, ctx, reason="System explanation")
        emit(ctx.interrogation_service.explain_system(args.project_id))
        return True
    if args.command == "lineage-report":
        emit(ctx.query_service.task_lineage(args.task_id))
        return True
    if args.command == "task-report":
        emit(ctx.query_service.task_report(args.task_id))
        return True
    if args.command == "explain-task":
        ensure_llm_ready(args, ctx, reason="Task explanation")
        emit(ctx.interrogation_service.explain_task(args.task_id))
        return True
    if args.command == "events":
        emit({"events": [serialize_dataclass(i) for i in store.list_events(args.entity_type, args.entity_id)]})
        return True
    return False
