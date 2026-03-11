from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

from .engine import HarnessEngine
from .store import SQLiteHarnessStore
from .telemetry import TelemetrySink

try:
    from temporalio import activity, workflow
    from temporalio.client import Client
    from temporalio.common import RetryPolicy
    from temporalio.worker import Worker
except ModuleNotFoundError:  # pragma: no cover - exercised via availability checks
    activity = None
    workflow = None
    Client = None
    RetryPolicy = None
    Worker = None


def _build_engine(db_path: str, workspace_root: str) -> HarnessEngine:
    store = SQLiteHarnessStore(Path(db_path))
    store.initialize()
    return HarnessEngine(
        store=store,
        workspace_root=Path(workspace_root),
        telemetry=TelemetrySink(Path(db_path).parent / "telemetry"),
    )


def _import_temporal_modules() -> tuple[Any, Any, Any]:
    if activity is None or workflow is None or Client is None or RetryPolicy is None:
        raise ModuleNotFoundError("temporalio is not installed")
    return activity, workflow, Client


def temporal_support_available() -> bool:
    try:
        _import_temporal_modules()
    except ModuleNotFoundError:
        return False
    return True


def task_to_stable_activity(config: dict[str, str], task_id: str) -> dict[str, object]:
    engine = _build_engine(config["db_path"], config["workspace_root"])
    runs = engine.run_until_stable(task_id)
    task = engine.store.get_task(task_id)
    return {"task_id": task_id, "task_status": task.status.value if task else None, "run_count": len(runs)}


def process_next_task_activity(
    config: dict[str, str],
    project_id: str | None = None,
    worker_id: str = "local-worker",
    lease_seconds: int = 300,
) -> dict[str, object] | None:
    engine = _build_engine(config["db_path"], config["workspace_root"])
    result = engine.process_next_task(
        project_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    if result is None:
        return None
    task = result["task"]
    runs = result["runs"]
    return {"task_id": task.id if task else None, "task_status": task.status.value if task else None, "run_count": len(runs)}

if activity is not None and workflow is not None:

    @activity.defn(name="task_to_stable_activity")
    async def task_to_stable_activity_defn(
        config: dict[str, str], task_id: str
    ) -> dict[str, object]:
        return await asyncio.to_thread(task_to_stable_activity, config, task_id)


    @activity.defn(name="process_next_task_activity")
    async def process_next_task_activity_defn(
        config: dict[str, str],
        project_id: str | None = None,
        worker_id: str = "local-worker",
        lease_seconds: int = 300,
    ) -> dict[str, object] | None:
        return await asyncio.to_thread(
            process_next_task_activity,
            config,
            project_id,
            worker_id,
            lease_seconds,
        )


    @workflow.defn(name="task_to_stable_workflow")
    class TaskToStableWorkflow:
        @workflow.run
        async def run(self, config: dict[str, str], task_id: str) -> dict[str, object]:
            return await workflow.execute_activity(
                "task_to_stable_activity",
                args=[config, task_id],
                start_to_close_timeout=timedelta(seconds=300),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )


    @workflow.defn(name="process_next_task_workflow")
    class ProcessNextTaskWorkflow:
        @workflow.run
        async def run(
            self,
            config: dict[str, str],
            project_id: str | None = None,
            worker_id: str = "local-worker",
            lease_seconds: int = 300,
        ) -> dict[str, object] | None:
            return await workflow.execute_activity(
                "process_next_task_activity",
                args=[config, project_id, worker_id, lease_seconds],
                start_to_close_timeout=timedelta(seconds=300),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )


def build_temporal_workflows() -> list[type]:
    _import_temporal_modules()
    return [TaskToStableWorkflow, ProcessNextTaskWorkflow]


async def run_temporal_worker(
    target: str,
    namespace: str,
    task_queue: str,
) -> None:
    workflows = build_temporal_workflows()
    _, _, client_cls = _import_temporal_modules()
    if Worker is None:
        raise ModuleNotFoundError("temporalio is not installed")

    activities = [task_to_stable_activity_defn, process_next_task_activity_defn]

    client = await client_cls.connect(target, namespace=namespace)
    worker = Worker(client, task_queue=task_queue, workflows=workflows, activities=activities)
    await worker.run()


def run_temporal_worker_sync(target: str, namespace: str, task_queue: str) -> None:
    asyncio.run(run_temporal_worker(target=target, namespace=namespace, task_queue=task_queue))
