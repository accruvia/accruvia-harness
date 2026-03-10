from __future__ import annotations

from ..domain import serialize_dataclass
from ..temporal_backend import run_temporal_worker_sync
from .common import CLIContext, emit


def handle_runtime_command(args, ctx: CLIContext) -> bool:
    config = ctx.config
    if args.command == "runtime-info":
        info = ctx.runtime.info()
        emit({"backend": info.backend, "available": info.available, "details": info.details})
        return True
    if args.command == "run-temporal-worker":
        run_temporal_worker_sync(
            target=config.temporal_target,
            namespace=config.temporal_namespace,
            task_queue=config.temporal_task_queue,
        )
        return True
    if args.command == "run-runtime":
        result = ctx.runtime.run_task_until_stable(args.task_id)
        emit({"task": serialize_dataclass(result["task"]), "runs": [serialize_dataclass(r) for r in result["runs"]]})
        return True
    if args.command == "process-next-runtime":
        result = ctx.runtime.process_next_task(args.project_id, worker_id=args.worker_id, lease_seconds=args.lease_seconds)
        emit({"processed": None} if result is None else {"task": serialize_dataclass(result["task"]), "runs": [serialize_dataclass(r) for r in result["runs"]]})
        return True
    return False
