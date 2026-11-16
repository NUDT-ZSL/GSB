from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router
from app.archive_site import SiteState
from app.archive_site import router as archive_router
from app.config import DEFAULT_BASE_URL, DEFAULT_DB_PATH, DEFAULT_HOST, DEFAULT_PORT
from app.db import Database
from app.service import TaskManager

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    *,
    db_path: str | Path | None = None,
    archive_base_url: str | None = None,
) -> FastAPI:
    resolved_db_path = str(db_path or DEFAULT_DB_PATH)
    resolved_archive_base_url = (archive_base_url or DEFAULT_BASE_URL).rstrip("/")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(resolved_db_path)
        app.state.db = db
        app.state.archive_site = SiteState()
        app.state.archive_base_url = resolved_archive_base_url
        app.state.task_manager = TaskManager(db, resolved_archive_base_url)
        yield
        await app.state.task_manager.shutdown()

    app = FastAPI(
        title="Internal Audit Incremental Archive Workbench",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(archive_router)
    app.include_router(api_router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/tasks/{task_id}", include_in_schema=False)
    async def task_page(task_id: str) -> FileResponse:
        return FileResponse(STATIC_DIR / "task.html")

    return app


app = create_app()


def run() -> None:
    port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    host = os.getenv("HOST", DEFAULT_HOST)
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("LOG_LEVEL", "info"))


if __name__ == "__main__":
    run()
