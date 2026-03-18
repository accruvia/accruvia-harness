from __future__ import annotations

from ..llm_availability import LLMAvailabilityGate
from ..store import SQLiteHarnessStore
from .run_service import RunService


class QueueService:
    def __init__(
        self,
        store: SQLiteHarnessStore,
        runner: RunService,
        llm_gate: LLMAvailabilityGate | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.llm_gate = llm_gate

    def process_next_task(
        self,
        project_id: str | None = None,
        worker_id: str = "local-worker",
        lease_seconds: int = 300,
        exclude_task_ids: set[str] | None = None,
        progress_callback=None,
    ) -> dict[str, object] | None:
        progress = progress_callback or (lambda _event: None)
        # Gate: refuse to start work if no LLM backend is reachable.
        if self.llm_gate is not None and not self.llm_gate.is_available():
            retry_in = self.llm_gate.seconds_until_retry
            progress({
                "type": "backends_unavailable",
                "message": f"No LLM backends reachable. Retry in {retry_in:.0f}s.",
                "probe_results": self.llm_gate.last_probe_results,
                "retry_in_seconds": retry_in,
            })
            return {"gate_blocked": True, "retry_in_seconds": retry_in}
        task = self.store.acquire_task_lease(
            worker_id,
            lease_seconds,
            project_id,
            exclude_task_ids=exclude_task_ids,
        )
        if task is None:
            return None
        progress = progress_callback or (lambda _event: None)
        backlog_before = self.store.metrics_snapshot(task.project_id)
        progress(
            {
                "type": "task_started",
                "task_id": task.id,
                "task_title": task.title,
                "project_id": task.project_id,
            }
        )
        try:
            run = self.runner.run_once(task.id, progress_callback=progress)
            runs = [run]
            updated_task = self.store.get_task(task.id)
            backlog_after = self.store.metrics_snapshot(task.project_id)
            progress(
                {
                    "type": "task_finished",
                    "task_id": task.id,
                    "task_title": task.title,
                    "project_id": task.project_id,
                    "status": updated_task.status.value if updated_task is not None else "unknown",
                    "run_id": run.id,
                    "run_status": run.status.value,
                    "summary": run.summary,
                    "backlog_before": backlog_before,
                    "backlog_after": backlog_after,
                }
            )
            return {"task": updated_task, "runs": runs}
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
        seen_task_ids: set[str] = set()
        for _ in range(limit):
            result = self.process_next_task(
                project_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                exclude_task_ids=seen_task_ids,
            )
            if result is None:
                break
            processed.append(result)
            seen_task_ids.add(result["task"].id)
        return processed
