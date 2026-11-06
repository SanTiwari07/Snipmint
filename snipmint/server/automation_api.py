"""HTTP routes for n8n automation.

- ``/api/integrations/n8n/*``: called by n8n. Authenticated with SNIPMINT_N8N_WEBHOOK_SECRET
  (``X-Snipmint-Secret`` header or an HMAC signature), rate limited, idempotent with an
  ``Idempotency-Key`` header. Not covered by SNIPMINT_TOKEN, so n8n never needs the app token.
- ``/api/automation/*``: called by the web app (normal app token). No secret is ever returned.

Everything is off (and harmless) unless SNIPMINT_N8N_WEBHOOK_SECRET is set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..automation import settings as auto_settings
from ..automation.events import EventDispatcher
from ..automation.security import RateLimiter, RedactingFilter, verify_request
from ..automation.service import AutomationOff, AutomationService
from ..automation.store import AutomationStore, ClipRef, Execution, JsonAutomationStore
from ..youtube.store import Privacy

log = logging.getLogger("snipmint.automation")

N8N_PREFIX = "/api/integrations/n8n"


class AutomationIntegration:
    def __init__(self, youtube, *, env: auto_settings.AutomationEnv | None = None,
                 store: AutomationStore | None = None, dispatcher: EventDispatcher | None = None):  # fmt: skip
        self.env = env or auto_settings.load()
        self.youtube = youtube
        self._store = store
        self._dispatcher = dispatcher
        self._service: AutomationService | None = None
        self.limiter = RateLimiter(self.env.rate_per_minute)
        self._filter: RedactingFilter | None = None

    @property
    def store(self) -> AutomationStore:
        if self._store is None:
            self._store = JsonAutomationStore()
        return self._store

    @property
    def dispatcher(self) -> EventDispatcher:
        if self._dispatcher is None:
            self._dispatcher = EventDispatcher(self.env, self.store)
        return self._dispatcher

    @property
    def service(self) -> AutomationService:
        if self._service is None:
            self._service = AutomationService(self.env, self.store, self.dispatcher, self.youtube)
        return self._service

    def startup(self, deliver: bool = True) -> None:
        if not self.env.configured:
            return
        self._filter = RedactingFilter(self.env.secret)
        self._filter.install()
        listeners = self.youtube.manager.listeners
        if self.service.on_item_finished not in listeners:
            listeners.append(self.service.on_item_finished)
        if deliver:
            self.dispatcher.start()

    def shutdown(self) -> None:
        if self._dispatcher is not None:
            self._dispatcher.stop()
        if self._filter is not None:
            self._filter.uninstall()
            self._filter = None


# ------------------------------------------------------------------ request models


class CreateTasks(BaseModel):
    clips: list[ClipRef] = Field(min_length=1, max_length=30)
    privacy: Privacy | None = None
    made_for_kids: bool | None = None
    run_at: datetime | None = Field(None, description="Publish at this time (with a time zone). Empty = now.")
    batch: bool = False
    delay_seconds: int | None = Field(None, ge=0, le=3600)
    concurrency: int | None = Field(None, ge=1, le=4)
    channel_id: str | None = None
    language: str | None = None


class SettingsUpdate(BaseModel):
    enabled: bool | None = None
    auto_approve: bool | None = None
    language: str | None = None
    privacy: Privacy | None = None
    made_for_kids: bool | None = None
    batch_delay_seconds: int | None = Field(None, ge=0, le=3600)
    batch_concurrency: int | None = Field(None, ge=1, le=4)
    max_attempts: int | None = Field(None, ge=1, le=10)
    max_lateness_minutes: int | None = Field(None, ge=5, le=10080)


class Reschedule(BaseModel):
    run_at: datetime


class ExecutionRef(BaseModel):
    execution_id: str | None = Field(None, max_length=120)


class MetadataRun(ExecutionRef):
    language: str | None = None


class ExecutionReport(BaseModel):
    execution_id: str = Field(min_length=1, max_length=120)
    workflow: str = Field("", max_length=120)
    status: str = Field(pattern=r"^(running|success|error|waiting|cancelled)$")
    task_id: str | None = Field(None, max_length=80)
    batch_id: str | None = Field(None, max_length=80)
    message: str = Field("", max_length=1000)


def register(app: FastAPI, youtube, *, integration: AutomationIntegration | None = None) -> AutomationIntegration:
    auto = integration or AutomationIntegration(youtube)
    app.state.automation = auto

    # ======================================================== n8n-facing routes

    n8n = APIRouter(prefix=N8N_PREFIX, tags=["n8n"])

    @app.middleware("http")
    async def n8n_guard(request: Request, call_next):
        path = request.url.path
        if not path.startswith(N8N_PREFIX + "/"):
            return await call_next(request)
        started = time.monotonic()
        client = request.client.host if request.client else "?"
        if not auto.env.configured:
            return JSONResponse({"error": auto.env.config_error or "The n8n integration is off (set SNIPMINT_N8N_WEBHOOK_SECRET).",
                                 "type": "NotConfigured", "details": None}, status_code=503)  # fmt: skip
        if not auto.limiter.allow(client):
            return JSONResponse({"error": "Too many requests.", "type": "RateLimited", "details": None},
                                status_code=429, headers={"Retry-After": "5"})  # fmt: skip
        body = await request.body()
        problem = verify_request(auto.env.secret, request.headers, body)
        if problem:
            log.warning("n8n request rejected: %s %s from %s (%s)", request.method, path, client, problem)
            return JSONResponse({"error": problem, "type": "Unauthorized", "details": None}, status_code=401)
        key = request.headers.get("idempotency-key")
        scoped = f"{request.method}:{path}:{key}" if key and request.method == "POST" else None
        if scoped:
            hit = auto.store.idempotent(scoped)
            if hit is not None:
                log.info("n8n %s %s replayed (idempotency key)", request.method, path)
                return JSONResponse(hit["body"], status_code=hit["status"], headers={"Idempotent-Replay": "true"})
        response = await call_next(request)
        if scoped and response.status_code < 500:
            raw = b"".join([chunk async for chunk in response.body_iterator])
            data = json.loads(raw or b"null")  # every route here answers JSON
            auto.store.remember(scoped, {"status": response.status_code, "body": data})
            response = JSONResponse(data, status_code=response.status_code)
        log.info("n8n %s %s -> %s in %.0f ms (execution %s)", request.method, path, response.status_code,
                 (time.monotonic() - started) * 1000, request.headers.get("x-n8n-execution-id", "-"))  # fmt: skip
        return response

    @n8n.get("/health")
    def n8n_health() -> dict[str, Any]:
        s = auto.service.settings
        return {"ok": True, "version": __version__, "automation_enabled": s.enabled,
                "youtube_configured": youtube.settings.configured}  # fmt: skip

    @n8n.get("/tasks/due")
    def n8n_due(limit: int = Query(25, ge=1, le=100)) -> list[dict[str, Any]]:
        return auto.service.due(limit)

    @n8n.get("/tasks/{task_id}")
    def n8n_task(task_id: str) -> dict[str, Any]:
        return auto.service.get(task_id)

    @n8n.post("/tasks/{task_id}/publish")
    async def n8n_publish(task_id: str, body: ExecutionRef) -> dict[str, Any]:
        return await asyncio.to_thread(auto.service.publish, task_id, body.execution_id)

    @n8n.post("/tasks/{task_id}/hold")
    def n8n_hold(task_id: str, body: ExecutionRef) -> dict[str, Any]:
        """Workflow A, "not approved" branch: mark the task as waiting for approval."""
        return auto.service.hold(task_id)

    @n8n.post("/tasks/{task_id}/metadata")
    async def n8n_metadata(task_id: str, body: MetadataRun) -> dict[str, Any]:
        # only the server's own Gemini key (if any) is used; n8n never handles AI keys
        key = (os.environ.get("GEMINI_API_KEY") or "").strip() or None
        return await asyncio.to_thread(auto.service.ensure_metadata, task_id, body.language, key)

    @n8n.get("/batches/{batch_id}")
    def n8n_batch(batch_id: str) -> dict[str, Any]:
        return auto.service.batch(batch_id)

    @n8n.post("/executions")
    def n8n_execution(report: ExecutionReport) -> dict[str, Any]:
        e = Execution(id=report.execution_id, workflow=report.workflow, status=report.status,  # type: ignore[arg-type]
                      task_id=report.task_id, batch_id=report.batch_id, message=report.message)  # fmt: skip
        return auto.service.record_execution(e).model_dump()

    app.include_router(n8n)

    # ========================================================== UI-facing routes

    @app.get("/api/automation/status", tags=["automation"])
    def status() -> dict[str, Any]:
        return auto.service.status()

    @app.put("/api/automation/settings", tags=["automation"])
    def update_settings(body: SettingsUpdate) -> dict[str, Any]:
        if not auto.env.configured:
            raise AutomationOff(auto.env.config_error)
        return auto.service.update_settings(body.model_dump(exclude_none=True)).model_dump()

    @app.post("/api/automation/tasks", tags=["automation"])
    def create_tasks(body: CreateTasks) -> dict[str, Any]:
        return auto.service.create(body.clips, privacy=body.privacy, made_for_kids=body.made_for_kids,
                                   run_at=body.run_at, batch=body.batch, delay_seconds=body.delay_seconds,
                                   concurrency=body.concurrency, channel_id=body.channel_id,
                                   language=body.language)  # fmt: skip

    @app.get("/api/automation/tasks", tags=["automation"])
    def list_tasks(limit: int = Query(50, ge=1, le=300)) -> list[dict[str, Any]]:
        if not auto.env.configured:
            return []
        return [auto.service.view(t) for t in auto.store.list_tasks()[:limit]]

    @app.get("/api/automation/batches/{batch_id}", tags=["automation"])
    def get_batch(batch_id: str) -> dict[str, Any]:
        return auto.service.batch(batch_id)

    @app.post("/api/automation/tasks/{task_id}/cancel", tags=["automation"])
    def cancel_task(task_id: str) -> dict[str, Any]:
        return auto.service.cancel(task_id)

    @app.post("/api/automation/tasks/{task_id}/retry", tags=["automation"])
    async def retry_task(task_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(auto.service.retry, task_id)

    @app.post("/api/automation/tasks/{task_id}/reschedule", tags=["automation"])
    def reschedule_task(task_id: str, body: Reschedule) -> dict[str, Any]:
        return auto.service.reschedule(task_id, body.run_at)

    @app.get("/api/automation/executions", tags=["automation"])
    def executions(limit: int = Query(30, ge=1, le=300)) -> list[dict[str, Any]]:
        if not auto.env.configured:
            return []
        return [e.model_dump() for e in auto.store.list_executions()[:limit]]

    @app.post("/api/automation/events/retry", tags=["automation"])
    def retry_events() -> dict[str, int]:
        if not auto.env.configured:
            raise AutomationOff(auto.env.config_error)
        return {"requeued": auto.dispatcher.retry_dead()}

    return auto
