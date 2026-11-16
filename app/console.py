"""FastAPI 控制台：任务创建、状态查询、暂停/恢复入口与简单前端页面。"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import collector
from .store import StateError, Store

STATIC = Path(__file__).parent / "static"


class TaskIn(BaseModel):
    config: dict = {}
    autostart: bool = True


def create_app(db_path: str, default_base_url: str) -> FastAPI:
    store = Store(db_path)
    app = FastAPI(title="动态页面采集控制台")
    workers: dict[int, asyncio.Task] = {}
    pause_flags: set[int] = set()

    @app.on_event("startup")
    async def _startup():
        # 进程重启后：遗留 running 任务只能依赖库中状态，迁移为 paused 等待恢复
        store.recover_orphans()

    def _launch(task_id: int, note: str):
        try:
            store.transition(task_id, "running", note=note)
        except StateError as e:
            raise HTTPException(
                status_code=409,
                detail={"code": e.code, "message": str(e),
                        "from": e.from_state, "to": e.to_state},
            )
        store.set_error(task_id, None, None)
        pause_flags.discard(task_id)
        workers[task_id] = asyncio.create_task(
            collector.run_task(store, task_id, pause_check=lambda: task_id in pause_flags))
        workers[task_id].add_done_callback(lambda _t, tid=task_id: workers.pop(tid, None))

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.post("/api/tasks", status_code=201)
    async def create_task(body: TaskIn):
        cfg = dict(body.config)
        cfg.setdefault("base_url", default_base_url)
        task_id = store.create_task(cfg)
        if body.autostart:
            _launch(task_id, "start")
        return {"task_id": task_id, "status": store.get_task(task_id)["status"]}

    @app.post("/api/tasks/{task_id}/start")
    async def start_task(task_id: int):
        if store.get_task(task_id) is None:
            raise HTTPException(404, {"code": "E_NOT_FOUND"})
        _launch(task_id, "start")
        return {"task_id": task_id, "status": "running"}

    @app.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: int):
        if store.get_task(task_id) is None:
            raise HTTPException(404, {"code": "E_NOT_FOUND"})
        _launch(task_id, "resume")
        return {"task_id": task_id, "status": "running"}

    @app.post("/api/tasks/{task_id}/pause")
    async def pause_task(task_id: int):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, {"code": "E_NOT_FOUND"})
        if task["status"] != "running":
            err = StateError(task_id, task["status"], "paused")
            raise HTTPException(409, {"code": err.code, "message": str(err)})
        pause_flags.add(task_id)
        return {"task_id": task_id, "status": "pausing"}

    @app.get("/api/tasks")
    async def list_tasks():
        return {"tasks": store.list_tasks()}

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: int):
        try:
            return store.summary(task_id)
        except KeyError:
            raise HTTPException(404, {"code": "E_NOT_FOUND"})

    @app.exception_handler(StateError)
    async def state_error_handler(_req, exc: StateError):
        return JSONResponse(status_code=409, content={"code": exc.code, "message": str(exc)})

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--db", default=os.environ.get("CONSOLE_DB_PATH", "console.db"))
    args = parser.parse_args()
    base_url = os.environ.get("MOCK_BASE_URL", "http://127.0.0.1:8100")
    uvicorn.run(create_app(args.db, base_url), host="127.0.0.1",
                port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
