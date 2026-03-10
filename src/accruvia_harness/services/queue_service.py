from __future__ import annotations

from ..store import SQLiteHarnessStore
from .run_service import RunService


class QueueService:
    def __init__(self, store: SQLiteHarnessStore, runner: RunService) -> None:
        self.store = store
        self.runner = runner

    def process_next_task(
        self,
        project_id: str | None = None,
        worker_id: str = "local-worker",
        lease_seconds: int = 300,
    ) -> dict[str, object] | None:
        task = self.store.acquire_task_lease(worker_id, lease_seconds, project_id)
        if task is None:
            return None
        try:
            runs = self.runner.run_until_stable(task.id)
            return {"task": self.store.get_task(task.id), "runs": runs}
        finally:
            self.store.release_task_lease(task.id, worker_id)

    def process_queue(
        self,
        limit: int,
        project_id: str | None = None,
        worker_id: str = "local-worker",
        lease_seconds: int = 300,
    ) -> list[dict[str, object]]:
        processed: list[dict[str, object]] = []
        for _ in range(limit):
            result = self.process_next_task(project_id, worker_id=worker_id, lease_seconds=lease_seconds)
            if result is None:
                break
            processed.append(result)
        return processed
