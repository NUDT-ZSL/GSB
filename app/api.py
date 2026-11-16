from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.config import MAX_CONCURRENCY, normalize_config
from app.db import Database
from app.service import TaskManager, create_task_id

router = APIRouter(prefix="/api", tags=["workbench-api"])


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(default="Archive crawl", min_length=1, max_length=120)
    wave: Literal["baseline", "incremental", "conflict"] = "baseline"
    scenario: str | None = None
    config: dict[str, Any] | None = None


def _manager(request: Request) -> TaskManager:
    return request.app.state.task_manager


def _db(request: Request) -> Database:
    return request.app.state.db


def _task_or_404(request: Request, task_id: str) -> dict[str, Any]:
    task = _db(request).get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": "E_NOT_FOUND", "message": "task not found"})
    return task


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    db = _db(request)
    return {
        "ok": True,
        "database": db.path,
        "archive_base_url": request.app.state.archive_base_url,
        "max_concurrency": MAX_CONCURRENCY,
        "task_count": len(db.list_tasks()),
    }


@router.post("/tasks", status_code=202)
async def create_task(payload: TaskCreate, request: Request) -> dict[str, Any]:
    extra = payload.model_extra or {}
    config: dict[str, Any] = {}
    if payload.config:
        config.update(payload.config)
    for key in (
        "concurrency",
        "delay_profile",
        "fault_profile",
        "render_timeout_s",
        "max_render_observations",
        "max_retries",
        "backoff_base_ms",
        "backoff_max_ms",
        "high_latency_ms",
        "max_session_replays",
        "request_timeout_s",
    ):
        if key in extra and extra[key] is not None:
            config[key] = extra[key]
    try:
        config = normalize_config(config)
        config["_archive_base_url"] = str(request.base_url).rstrip("/")
    except (TypeError, ValueError) as exc:
        code = "E_CONCURRENCY_LIMIT" if "concurrency" in str(exc) else "E_CONFIG"
        raise HTTPException(status_code=400, detail={"code": code, "message": str(exc)}) from exc
    task_id = create_task_id()
    scenario = payload.scenario or payload.wave
    _db(request).create_task(task_id, payload.name, scenario, payload.wave, config)
    launched = _manager(request).launch(task_id)
    if not launched:
        raise HTTPException(
            status_code=500,
            detail={"code": "E_TASK_START", "message": "task could not be started"},
        )
    return {
        "ok": True,
        "task_id": task_id,
        "status": "running",
        "wave": payload.wave,
        "config": config,
    }


@router.get("/tasks")
async def list_tasks(request: Request) -> dict[str, Any]:
    tasks = _db(request).list_tasks()
    for task in tasks:
        task["is_running"] = _manager(request).is_running(task["id"])
    return {"ok": True, "tasks": tasks}


@router.get("/tasks/{task_id}")
async def task_detail(task_id: str, request: Request) -> dict[str, Any]:
    task = _task_or_404(request, task_id)
    db = _db(request)
    return {
        "ok": True,
        "task": task,
        "progress": db.task_progress(task_id),
        "checkpoint": db.get_checkpoint(task_id),
        "sessions": db.sessions(task_id),
        "is_running": _manager(request).is_running(task_id),
        "recent_events": db.request_events(task_id, limit=80),
        "items": db.task_items(task_id, limit=200),
    }


@router.get("/tasks/{task_id}/progress")
async def task_progress(task_id: str, request: Request) -> dict[str, Any]:
    task = _task_or_404(request, task_id)
    return {
        "ok": True,
        "task_id": task_id,
        "status": task["status"],
        "is_running": _manager(request).is_running(task_id),
        "progress": _db(request).task_progress(task_id),
        "checkpoint": _db(request).get_checkpoint(task_id),
    }


@router.get("/tasks/{task_id}/differences")
async def task_differences(task_id: str, request: Request) -> dict[str, Any]:
    _task_or_404(request, task_id)
    return {"ok": True, **_db(request).task_differences(task_id)}


@router.get("/tasks/{task_id}/graph")
async def task_graph(task_id: str, request: Request) -> dict[str, Any]:
    _task_or_404(request, task_id)
    return {"ok": True, **_db(request).task_graph(task_id)}


@router.get("/tasks/{task_id}/snapshots")
async def task_snapshots(task_id: str, request: Request, page: int | None = None) -> dict[str, Any]:
    _task_or_404(request, task_id)
    return {"ok": True, "snapshots": _db(request).render_snapshots(task_id, page)}


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request, limit: int = 500) -> dict[str, Any]:
    _task_or_404(request, task_id)
    return {"ok": True, "events": _db(request).request_events(task_id, limit=min(limit, 2000))}


@router.post("/tasks/{task_id}/resume", status_code=202)
async def resume_task(task_id: str, request: Request) -> dict[str, Any]:
    task = _task_or_404(request, task_id)
    if _manager(request).is_running(task_id):
        raise HTTPException(
            status_code=409,
            detail={"code": "E_ALREADY_RUNNING", "message": "task is already running"},
        )
    if task["status"] == "completed":
        raise HTTPException(
            status_code=409,
            detail={"code": "E_NOTHING_TO_RESUME", "message": "task is already complete"},
        )
    if not _manager(request).launch(task_id, resuming=True):
        raise HTTPException(
            status_code=409,
            detail={"code": "E_NOT_RESUMABLE", "message": "task state is not resumable"},
        )
    return {"ok": True, "task_id": task_id, "status": "running", "resumed": True}


@router.get("/business/{business_id}")
async def business_chain(business_id: str, request: Request) -> dict[str, Any]:
    chain = _db(request).business_chain(business_id)
    if chain is None:
        raise HTTPException(status_code=404, detail={"code": "E_NOT_FOUND", "message": "business id not found"})
    return {"ok": True, **chain}


@router.get("/raw/{raw_id}")
async def raw_response(raw_id: int, request: Request) -> dict[str, Any]:
    raw = _db(request).get_raw_response(raw_id)
    if raw is None:
        raise HTTPException(status_code=404, detail={"code": "E_NOT_FOUND", "message": "raw response not found"})
    return {"ok": True, "raw": raw}


@router.get("/consistency")
async def consistency(request: Request) -> dict[str, Any]:
    return {"ok": True, **_db(request).global_consistency_summary()}
