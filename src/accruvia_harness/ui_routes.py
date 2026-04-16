"""FastAPI routes and server entry point for the harness UI.

Extracted from ui.py to reduce monolith size. Contains:
- _EventBus (SSE pub/sub)
- _build_fastapi_app (34 route handlers)
- start_ui_server, _auto_start_supervisors, _verify_install_path, _resolve_ui_port
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import threading
from pathlib import Path
from queue import Empty, Queue
from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from .ui import HarnessUIDataService


class _EventBus:
    """Simple pub/sub for SSE. Clients register a queue; writers broadcast."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Queue[str | None]] = []

    def subscribe(self) -> Queue[str | None]:
        q: Queue[str | None] = Queue(maxsize=32)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: Queue[str | None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event: str) -> None:
        with self._lock:
            dead: list[Queue[str | None]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            for q in dead:
                try:
                    self._subscribers.remove(q)
                except ValueError:
                    pass


def _build_fastapi_app(data_service: "HarnessUIDataService", event_bus: _EventBus):
    """Build a FastAPI application wired to the given data service and event bus."""
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse

    from .ui import _GIT_COMMIT, _SERVER_STARTED_AT

    class _JSONResponse(JSONResponse):
        def render(self, content) -> bytes:
            return json.dumps(content, indent=2, sort_keys=True).encode("utf-8")

    app = FastAPI(title="Accruvia Harness", default_response_class=_JSONResponse)
    _NOCACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    cors_origins = tuple(
        origin.strip()
        for origin in os.environ.get(
            "ACCRUVIA_UI_CORS_ORIGINS",
            "http://127.0.0.1:3000,http://localhost:3000,http://127.0.0.1:4173,http://localhost:4173",
        ).split(",")
        if origin.strip()
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _dispatch(fn, *, status_code: int = 200, notify: bool = False):
        try:
            payload = fn()
        except ValueError as exc:
            return _JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return _JSONResponse({"error": str(exc)}, status_code=500)
        if notify:
            data_service.invalidate_harness_overview_cache()
            event_bus.publish("workspace-changed")
        return _JSONResponse(payload, status_code=status_code)

    @app.middleware("http")
    async def nocache_middleware(request, call_next):
        response = await call_next(request)
        for k, v in _NOCACHE.items():
            response.headers[k] = v
        return response

    @app.get("/")
    def index():
        return {
            "service": "accruvia-harness-api",
            "commit": _GIT_COMMIT,
            "started_at": _SERVER_STARTED_AT,
            "docs_url": "/docs",
        }

    @app.get("/api/projects")
    def list_projects():
        return data_service.list_projects()

    @app.get("/api/projects/{project_ref}/workspace")
    def project_workspace(project_ref: str):
        return _dispatch(lambda: data_service.project_workspace(project_ref))

    @app.get("/api/projects/{project_ref}/summary")
    def project_summary(project_ref: str):
        return _dispatch(lambda: data_service.project_summary_fast(project_ref))

    @app.get("/api/projects/{project_ref}/objectives")
    def project_objectives(project_ref: str):
        return _dispatch(lambda: data_service.project_objectives_detail(project_ref))

    @app.get("/api/projects/{project_ref}/objectives/{objective_id}")
    def project_objective_detail(project_ref: str, objective_id: str):
        return _dispatch(lambda: data_service.project_objective_detail(project_ref, objective_id))

    @app.get("/api/projects/{project_ref}/token-performance")
    def project_token_performance(project_ref: str):
        return _dispatch(lambda: data_service.project_token_performance(project_ref))

    @app.get("/api/version")
    def version():
        return {"commit": _GIT_COMMIT, "started_at": _SERVER_STARTED_AT}

    @app.get("/api/harness")
    def harness_overview():
        return data_service.harness_overview()

    @app.get("/api/atomicity")
    def harness_atomicity():
        return _dispatch(lambda: data_service.harness_atomicity_overview())

    @app.get("/api/promotion")
    def harness_promotion():
        return _dispatch(lambda: data_service.harness_promotion_overview())

    @app.get("/api/runs/{run_id}/cli-output")
    def run_cli_output(run_id: str):
        return _dispatch(lambda: data_service.run_cli_output(run_id))

    @app.get("/api/tasks/{task_id}/insight")
    def task_insight(task_id: str):
        return _dispatch(lambda: data_service.task_failure_insight(task_id))

    @app.get("/api/projects/{project_id}/supervisor")
    def supervisor_status(project_id: str):
        return data_service.supervisor_status(project_id)

    @app.get("/api/events")
    async def sse_events():
        async def event_stream():
            q = event_bus.subscribe()
            try:
                while True:
                    try:
                        event = await asyncio.to_thread(q.get, timeout=15)
                    except Empty:
                        yield ":\n\n"
                        continue
                    if event is None:
                        break
                    yield f"data: {event}\n\n"
            except (asyncio.CancelledError, GeneratorExit):
                pass
            finally:
                event_bus.unsubscribe(q)
        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    @app.post("/api/projects/{project_id}/repo-settings")
    async def update_repo_settings(project_id: str, request):
        payload = await request.json()
        return _dispatch(lambda: data_service.update_project_repo_settings(
            project_id, promotion_mode=str(payload.get("promotion_mode") or ""),
            repo_provider=str(payload.get("repo_provider") or ""), repo_name=str(payload.get("repo_name") or ""),
            base_branch=str(payload.get("base_branch") or ""),
        ), notify=True)

    @app.post("/api/projects/{project_ref}/objectives", status_code=201)
    async def create_objective(project_ref: str, request: Request):
        payload = await request.json()
        return _dispatch(lambda: data_service.create_objective(
            project_ref, str(payload.get("title") or ""), str(payload.get("summary") or ""),
        ), status_code=201, notify=True)

    @app.post("/api/objectives/{objective_id}/tasks", status_code=201)
    def create_linked_task(objective_id: str):
        return _dispatch(lambda: data_service.create_linked_task(objective_id), status_code=201, notify=True)

    @app.post("/api/objectives/{objective_id}/interrogation", status_code=201)
    def complete_interrogation(objective_id: str):
        return _dispatch(lambda: data_service.complete_interrogation_review(objective_id), status_code=201, notify=True)

    @app.post("/api/objectives/{objective_id}/promotion/force", status_code=201)
    async def force_promote(objective_id: str, request: Request):
        payload = await request.json()
        return _dispatch(lambda: data_service.force_promote_objective_review(
            objective_id, rationale=str(payload.get("rationale") or ""), author=str(payload.get("author") or "operator"),
        ), status_code=201, notify=True)

    @app.post("/api/objectives/{objective_id}/promote")
    def promote_objective(objective_id: str):
        return _dispatch(lambda: data_service.promote_objective_to_repo(objective_id), notify=True)

    @app.post("/api/tasks/{task_id}/promote")
    def promote_task(task_id: str):
        return _dispatch(lambda: data_service.promote_atomic_unit_to_repo(task_id), notify=True)

    @app.post("/api/objectives/{objective_id}/mermaid/proposal/accept", status_code=201)
    async def accept_mermaid(objective_id: str, request):
        payload = await request.json()
        return _dispatch(lambda: data_service.accept_mermaid_proposal(
            objective_id, str(payload.get("proposal_id") or ""),
        ), status_code=201, notify=True)

    @app.post("/api/objectives/{objective_id}/mermaid/proposal/reject", status_code=201)
    async def reject_mermaid(objective_id: str, request):
        payload = await request.json()
        return _dispatch(lambda: data_service.reject_mermaid_proposal(
            objective_id, str(payload.get("proposal_id") or ""),
            resolution=str(payload.get("resolution") or "refine"),
        ), status_code=201, notify=True)

    @app.post("/api/tasks/{task_id}/run", status_code=201)
    def run_task(task_id: str):
        return _dispatch(lambda: data_service.run_task(task_id), status_code=201, notify=True)

    @app.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        return _dispatch(lambda: data_service.retry_task(task_id), notify=True)

    @app.post("/api/tasks/{task_id}/failed-disposition")
    async def failed_task_disposition(task_id: str, request: Request):
        payload = await request.json()
        return _dispatch(
            lambda: data_service.apply_failed_task_disposition(
                task_id,
                disposition=str(payload.get("disposition") or ""),
                rationale=str(payload.get("rationale") or ""),
            ),
            notify=True,
        )

    @app.post("/api/projects/{project_id}/supervise", status_code=201)
    def start_supervisor(project_id: str):
        return _dispatch(lambda: data_service.start_supervisor(project_id), status_code=201, notify=True)

    @app.post("/api/projects/{project_id}/supervise/stop")
    def stop_supervisor(project_id: str):
        return _dispatch(lambda: data_service.stop_supervisor(project_id), notify=True)

    @app.post("/api/cli/command", status_code=201)
    async def cli_command(request):
        payload = await request.json()
        return _dispatch(lambda: data_service.run_cli_command(str(payload.get("command") or "")), status_code=201, notify=True)

    @app.post("/api/projects/{project_id}/retry-failed")
    def retry_all_failed(project_id: str):
        return _dispatch(lambda: data_service.retry_all_failed(project_id), notify=True)

    @app.put("/api/objectives/{objective_id}/mermaid")
    async def update_mermaid(objective_id: str, request):
        payload = await request.json()
        return _dispatch(lambda: data_service.update_mermaid_artifact(
            objective_id, status=str(payload.get("status") or ""),
            summary=str(payload.get("summary") or ""), blocking_reason=str(payload.get("blocking_reason") or ""),
        ), notify=True)

    @app.put("/api/objectives/{objective_id}/intent")
    async def update_intent(objective_id: str, request: Request):
        payload = await request.json()
        return _dispatch(lambda: data_service.update_intent_model(
            objective_id, intent_summary=str(payload.get("intent_summary") or ""),
            success_definition=str(payload.get("success_definition") or ""),
            non_negotiables=list(payload.get("non_negotiables") or []),
            frustration_signals=list(payload.get("frustration_signals") or []),
        ), notify=True)

    return app


def _verify_install_path() -> None:
    import accruvia_harness
    installed = Path(accruvia_harness.__file__).resolve().parent
    expected = Path(__file__).resolve().parent
    if installed != expected:
        raise RuntimeError(
            f"Installed package points to {installed}, expected {expected}. "
            f"Run: pip install -e . from the project root."
        )


def start_ui_server(ctx, *, host: str, port: int, open_browser: bool, project_ref: str | None = None) -> None:
    from .ui import (
        HarnessUIDataService,
        _BACKGROUND_SUPERVISOR,
        _GIT_COMMIT,
        resolve_project_ref,
        update_ui_runtime_state,
        clear_ui_runtime_state,
    )

    _verify_install_path()
    if hasattr(ctx, "config") and ctx.config is not None:
        from .llm_availability import LLMAvailabilityGate
        from .onboarding import probe_llm_command
        gate = LLMAvailabilityGate(
            probe_fn=probe_llm_command,
            commands=[
                ("codex", ctx.config.llm_codex_command or ""),
                ("claude", ctx.config.llm_claude_command or ""),
                ("command", ctx.config.llm_command or ""),
            ],
        )
        ctx.engine.set_llm_gate(gate)
    data_service = HarnessUIDataService(ctx)
    if hasattr(ctx, "engine") and hasattr(ctx.engine, "queue"):
        ctx.engine.queue.post_task_callback = data_service.reconcile_task_workflow
    resolved_port = _resolve_ui_port(host, port)
    event_bus = _EventBus()
    app = _build_fastapi_app(data_service, event_bus)
    url = f"http://{host}:{resolved_port}/"
    if project_ref:
        project_id = resolve_project_ref(ctx, project_ref)
        url = f"{url}?project_id={project_id}"
    update_ui_runtime_state(
        ctx.config,
        host=host,
        preferred_port=port,
        resolved_port=resolved_port,
        project_ref=project_ref,
    )
    print(f"Harness API running at {url} (commit {_GIT_COMMIT})", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        print("Run the frontend separately with `npm --prefix frontend run dev`.", flush=True)
    _stop_change_detector = threading.Event()

    def _detect_changes() -> None:
        last_signature: str | None = None
        while not _stop_change_detector.wait(timeout=3):
            try:
                tasks = data_service.store.list_tasks()
                records = data_service.store.list_context_records()
                recent_records = records[-20:]
                sig = ";".join(
                    f"{t.id}:{t.status.value}:{t.updated_at.isoformat()}" for t in tasks
                )
                sig += "|ctx:" + ";".join(
                    f"{r.id}:{r.record_type}:{r.created_at.isoformat()}" for r in recent_records
                )
                if last_signature is not None and sig != last_signature:
                    data_service.invalidate_harness_overview_cache()
                    event_bus.publish("workspace-changed")
                last_signature = sig
            except Exception:
                pass

    change_thread = threading.Thread(target=_detect_changes, daemon=True)
    change_thread.start()

    _auto_start_supervisors(data_service, ctx)
    import uvicorn
    try:
        uvicorn.run(app, host=host, port=resolved_port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        clear_ui_runtime_state(ctx.config)
        _stop_change_detector.set()
        for project in data_service.store.list_projects():
            _BACKGROUND_SUPERVISOR.stop(project.id)


def _auto_start_supervisors(data_service: "HarnessUIDataService", ctx) -> None:
    from .ui import _BACKGROUND_SUPERVISOR

    external_supervisors_present = any(data_service._live_supervisor_records(project.id) for project in data_service.store.list_projects())
    cleared = 0
    recovered = {"runs": 0, "tasks": 0, "leases": 0}
    if not external_supervisors_present:
        with data_service.store.connect() as connection:
            cleared = connection.execute("DELETE FROM task_leases").rowcount
        recovered = data_service.store.recover_stale_state()
        if cleared or any(int(count or 0) > 0 for count in recovered.values()):
            print(f"  Startup recovery: cleared {cleared} leases, recovered {recovered}", flush=True)
    for project in data_service.store.list_projects():
        for objective in data_service.store.list_objectives(project.id):
            try:
                data_service.reconcile_objective_workflow(objective.id)
                data_service._maybe_resume_atomic_generation(objective.id)
                data_service._maybe_resume_objective_review(objective.id)
            except Exception:
                pass
        metrics = data_service.store.metrics_snapshot(project.id)
        pending = int(metrics.get("tasks_by_status", {}).get("pending", 0))
        active = int(metrics.get("tasks_by_status", {}).get("active", 0))
        if data_service._live_supervisor_records(project.id):
            continue
        if pending + active > 0:
            started = _BACKGROUND_SUPERVISOR.start(project.id, ctx.engine, watch=True)
            if started:
                print(f"  Auto-started harness for {project.name} ({pending} pending, {active} active)", flush=True)


def _resolve_ui_port(host: str, preferred_port: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, preferred_port))
        except OSError as exc:
            if exc.errno in {errno.EADDRINUSE, 48, 98}:
                raise OSError(
                    f"UI port {preferred_port} is already in use on {host}. "
                    "Refusing to fall back to another port because the control plane requires a single canonical API endpoint."
                ) from exc
            raise
    return preferred_port
