from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from .bootstrap import build_engine_from_config
from .config import HarnessConfig
from .domain import Run, RunStatus, new_id
from .engine import HarnessEngine

try:
    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
    from temporalio import activity, workflow
    from temporalio.client import Client
    from temporalio.common import RetryPolicy
    from temporalio.worker import Worker
except ModuleNotFoundError:  # pragma: no cover - exercised via availability checks
    DescribeNamespaceRequest = None
    activity = None
    workflow = None
    Client = None
    RetryPolicy = None
    Worker = None


TEMPORAL_CONNECT_ATTEMPTS = 30
TEMPORAL_CONNECT_DELAY_SECONDS = 1.0


def _build_engine(config_payload: str | dict[str, object]) -> HarnessEngine:
    if isinstance(config_payload, str):
        config = HarnessConfig.from_json(config_payload)
    else:
        config = HarnessConfig.from_payload(config_payload)
    return build_engine_from_config(config)


def _load_config(config_payload: str | dict[str, object]) -> HarnessConfig:
    if isinstance(config_payload, str):
        return HarnessConfig.from_json(config_payload)
    return HarnessConfig.from_payload(config_payload)


def _task_to_stable_timeout_seconds(config_payload: str | dict[str, object]) -> int:
    config = _load_config(config_payload)
    return max(300, config.timeout_max_seconds + 60)


def _process_next_timeout_seconds(
    config_payload: str | dict[str, object],
    lease_seconds: int,
) -> int:
    config = _load_config(config_payload)
    return max(300, config.timeout_max_seconds + max(lease_seconds, 0) + 60)


def _task_runtime_budget_seconds(config_payload: str | dict[str, object], task_id: str) -> int:
    config = _load_config(config_payload)
    engine = _build_engine(config_payload)
    task = engine.store.get_task(task_id)
    if task is None:
        return _task_to_stable_timeout_seconds(config_payload)
    max_attempts = max(task.max_attempts, 1)
    max_branches = max(task.max_branches, 1)
    branch_budget = config.timeout_max_seconds * max_branches if max_branches > 1 else 0
    return max(
        300,
        (config.timeout_max_seconds * max_attempts) + branch_budget + (60 * max_attempts),
    )


def _next_task_runtime_budget_seconds(
    config_payload: str | dict[str, object],
    project_id: str | None,
    lease_seconds: int,
) -> int:
    config = _load_config(config_payload)
    engine = _build_engine(config_payload)
    task = engine.store.next_pending_task(project_id)
    if task is None:
        return _process_next_timeout_seconds(config_payload, lease_seconds)
    max_attempts = max(task.max_attempts, 1)
    max_branches = max(task.max_branches, 1)
    branch_budget = config.timeout_max_seconds * max_branches if max_branches > 1 else 0
    return max(
        300,
        (config.timeout_max_seconds * max_attempts) + branch_budget + max(lease_seconds, 0) + (60 * max_attempts),
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


async def connect_temporal_client(
    client_cls: Any,
    target: str,
    namespace: str,
    *,
    attempts: int = TEMPORAL_CONNECT_ATTEMPTS,
    delay_seconds: float = TEMPORAL_CONNECT_DELAY_SECONDS,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(max(attempts, 1)):
        try:
            client = await client_cls.connect(target, namespace=namespace)
            if DescribeNamespaceRequest is not None:
                await client.service_client.workflow_service.describe_namespace(
                    DescribeNamespaceRequest(namespace=namespace)
                )
            return client
        except Exception as exc:  # pragma: no cover - covered via runtime tests
            last_error = exc
            if attempt == max(attempts, 1) - 1:
                raise
            await asyncio.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


def task_to_stable_activity(config: str, task_id: str) -> dict[str, object]:
    engine = _build_engine(config)
    runs = engine.run_until_stable(task_id)
    task = engine.store.get_task(task_id)
    return {"task_id": task_id, "task_status": task.status.value if task else None, "run_count": len(runs)}


def create_run_activity(config: str, task_id: str, attempt: int) -> dict[str, object]:
    cfg = _load_config(config)
    engine = _build_engine(config)
    run = Run(
        id=new_id("run"),
        task_id=task_id,
        status=RunStatus.PLANNING,
        attempt=attempt,
        summary="",
    )
    engine.store.create_run(run)
    return {"run_id": run.id, "workspace_root": str(cfg.workspace_root)}


def process_next_task_activity(
    config: str,
    project_id: str | None = None,
    worker_id: str = "local-worker",
    lease_seconds: int = 300,
) -> dict[str, object] | None:
    engine = _build_engine(config)
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
    async def task_to_stable_activity_defn(config: str, task_id: str) -> dict[str, object]:
        return await asyncio.to_thread(task_to_stable_activity, config, task_id)


    @activity.defn(name="create_run_activity")
    async def create_run_activity_defn(config: str, task_id: str, attempt: int) -> dict[str, object]:
        return await asyncio.to_thread(create_run_activity, config, task_id, attempt)

    @activity.defn(name="process_next_task_activity")
    async def process_next_task_activity_defn(
        config: str,
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
        async def run(self, config: str, task_id: str, activity_timeout_seconds: int) -> dict[str, object]:
            return await workflow.execute_activity(
                "task_to_stable_activity",
                args=[config, task_id],
                start_to_close_timeout=timedelta(seconds=activity_timeout_seconds),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )


    @workflow.defn(name="process_next_task_workflow")
    class ProcessNextTaskWorkflow:
        @workflow.run
        async def run(
            self,
            config: str,
            project_id: str | None = None,
            worker_id: str = "local-worker",
            lease_seconds: int = 300,
            activity_timeout_seconds: int = 300,
        ) -> dict[str, object] | None:
            return await workflow.execute_activity(
                "process_next_task_activity",
                args=[config, project_id, worker_id, lease_seconds],
                start_to_close_timeout=timedelta(seconds=activity_timeout_seconds),
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

    activities = [task_to_stable_activity_defn, create_run_activity_defn, process_next_task_activity_defn]

    client = await connect_temporal_client(client_cls, target, namespace)
    worker = Worker(client, task_queue=task_queue, workflows=workflows, activities=activities)
    await worker.run()


def run_temporal_worker_sync(target: str, namespace: str, task_queue: str) -> None:
    asyncio.run(run_temporal_worker(target=target, namespace=namespace, task_queue=task_queue))
