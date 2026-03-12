from __future__ import annotations

from dataclasses import dataclass
import asyncio
from uuid import uuid4
from typing import Protocol

from .config import HarnessConfig
from .engine import HarnessEngine
from .temporal_backend import (
    _next_task_runtime_budget_seconds,
    _task_runtime_budget_seconds,
    connect_temporal_client,
    temporal_support_available,
)


def _get_temporal_client_class():
    from temporalio.client import Client

    return Client


@dataclass(slots=True)
class RuntimeInfo:
    backend: str
    available: bool
    details: dict[str, object]


class WorkflowRuntime(Protocol):
    def info(self) -> RuntimeInfo: ...

    def run_task_until_stable(self, task_id: str) -> dict[str, object]: ...

    def process_next_task(
        self,
        project_id: str | None = None,
        worker_id: str = "local-worker",
        lease_seconds: int = 300,
    ) -> dict[str, object] | None: ...


@dataclass(slots=True)
class LocalWorkflowRuntime:
    engine: HarnessEngine

    def info(self) -> RuntimeInfo:
        return RuntimeInfo(
            backend="local",
            available=True,
            details={"mode": "synchronous", "durable_runtime": False},
        )

    def run_task_until_stable(self, task_id: str) -> dict[str, object]:
        runs = self.engine.run_until_stable(task_id)
        task = self.engine.store.get_task(task_id)
        return {"task": task, "runs": runs}

    def process_next_task(
        self,
        project_id: str | None = None,
        worker_id: str = "local-worker",
        lease_seconds: int = 300,
    ) -> dict[str, object] | None:
        return self.engine.process_next_task(
            project_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )


@dataclass(slots=True)
class TemporalWorkflowRuntime:
    config: HarnessConfig
    engine: HarnessEngine
    target: str
    namespace: str
    task_queue: str

    def info(self) -> RuntimeInfo:
        if not temporal_support_available():
            return RuntimeInfo(
                backend="temporal",
                available=False,
                details={
                    "target": self.target,
                    "namespace": self.namespace,
                    "task_queue": self.task_queue,
                    "reason": "temporalio_not_installed",
                },
            )
        return RuntimeInfo(
            backend="temporal",
            available=True,
            details={
                "target": self.target,
                "namespace": self.namespace,
                "task_queue": self.task_queue,
                "mode": "workflow_submission_ready",
            },
        )

    def run_task_until_stable(self, task_id: str) -> dict[str, object]:
        info = self.info()
        if not info.available:
            raise RuntimeError("Temporal runtime is not available in this environment")
        return asyncio.run(self._run_task_until_stable(task_id))

    def process_next_task(
        self,
        project_id: str | None = None,
        worker_id: str = "local-worker",
        lease_seconds: int = 300,
    ) -> dict[str, object] | None:
        info = self.info()
        if not info.available:
            raise RuntimeError("Temporal runtime is not available in this environment")
        return asyncio.run(self._process_next_task(project_id, worker_id, lease_seconds))

    async def _run_task_until_stable(self, task_id: str) -> dict[str, object]:
        client_cls = _get_temporal_client_class()
        client = await connect_temporal_client(client_cls, self.target, self.namespace)
        timeout_seconds = _task_runtime_budget_seconds(self.config.to_json(), task_id)
        await client.execute_workflow(
            "task_to_stable_workflow",
            args=[self.config.to_json(), task_id, timeout_seconds],
            id=f"task-to-stable-{task_id}-{uuid4().hex[:8]}",
            task_queue=self.task_queue,
        )
        task = self.engine.store.get_task(task_id)
        runs = self.engine.store.list_runs(task_id)
        return {"task": task, "runs": runs}

    async def _process_next_task(
        self,
        project_id: str | None = None,
        worker_id: str = "local-worker",
        lease_seconds: int = 300,
    ) -> dict[str, object] | None:
        client_cls = _get_temporal_client_class()
        client = await connect_temporal_client(client_cls, self.target, self.namespace)
        timeout_seconds = _next_task_runtime_budget_seconds(self.config.to_json(), project_id, lease_seconds)
        result = await client.execute_workflow(
            "process_next_task_workflow",
            args=[self.config.to_json(), project_id, worker_id, lease_seconds, timeout_seconds],
            id=f"process-next-{project_id or 'global'}-{uuid4().hex[:8]}",
            task_queue=self.task_queue,
        )
        if result is None:
            return None
        task_id = result["task_id"]
        task = self.engine.store.get_task(task_id)
        runs = self.engine.store.list_runs(task_id)
        return {"task": task, "runs": runs}


def build_runtime(
    backend: str,
    config: HarnessConfig,
    engine: HarnessEngine,
    temporal_target: str,
    temporal_namespace: str,
    temporal_task_queue: str,
) -> WorkflowRuntime:
    if backend == "local":
        return LocalWorkflowRuntime(engine=engine)
    if backend == "temporal":
        return TemporalWorkflowRuntime(
            config=config,
            engine=engine,
            target=temporal_target,
            namespace=temporal_namespace,
            task_queue=temporal_task_queue,
        )
    raise ValueError(f"Unsupported runtime backend: {backend}")
