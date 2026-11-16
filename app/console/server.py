"""Interactive operator console: create crawl tasks, watch progress and
failures, edit record fields with version history, resolve conflicts and
trigger resume. Shares the normalized field set and version semantics with
the crawler through the storage layer."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import CrawlerConfig
from ..crawler.crawler import Crawler, SimulatedCrash
from ..crawler.parser import SHARED_FIELDS
from ..storage.db import Store

TEMPLATE_DIR = Path(__file__).parent / "templates"


def create_console_app(db_path: str, default_site_url: str) -> FastAPI:
    store = Store(db_path)
    app = FastAPI(title="uid8-console")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    running: dict = {}

    def launch(task_id: str, cfg: CrawlerConfig):
        crawler = Crawler(store, cfg, task_id)

        async def runner():
            try:
                await crawler.run()
            except SimulatedCrash:
                pass

        running[task_id] = asyncio.create_task(runner())

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request=request, name="tasks.html", context={
            "tasks": store.list_tasks(),
            "default_site_url": default_site_url,
        })

    @app.post("/tasks")
    async def create_task(task_id: str = Form(...), site_url: str = Form(default=""),
                          max_inflight: int = Form(default=8),
                          initial_inflight: int = Form(default=4)):
        cfg = CrawlerConfig(site_url=site_url or default_site_url, db_path=db_path)
        cfg.scheduler.max_inflight = max_inflight
        cfg.scheduler.initial_inflight = initial_inflight
        store.create_task(task_id)
        launch(task_id, cfg)
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: str):
        return templates.TemplateResponse(request=request, name="task_detail.html", context={
            "task": store.get_task(task_id),
            "failures": store.list_failures(task_id),
            "running": task_id in running and not running[task_id].done(),
        })

    @app.post("/tasks/{task_id}/resume")
    async def resume_task(task_id: str, site_url: str = Form(default="")):
        task = store.get_task(task_id)
        if task is None:
            return RedirectResponse("/", status_code=303)
        cfg = CrawlerConfig(site_url=site_url or default_site_url, db_path=db_path)
        launch(task_id, cfg)
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    @app.get("/records", response_class=HTMLResponse)
    async def records(request: Request):
        return templates.TemplateResponse(request=request, name="records.html", context={
            "records": store.list_records(),
        })

    @app.get("/records/{item_key}", response_class=HTMLResponse)
    async def record_detail(request: Request, item_key: str):
        return templates.TemplateResponse(request=request, name="record_detail.html", context={
            "item_key": item_key,
            "latest": store.latest_version(item_key),
            "versions": store.versions_of(item_key),
            "shared_fields": SHARED_FIELDS,
        })

    @app.post("/records/{item_key}/edit")
    async def edit_record(request: Request, item_key: str):
        form = await request.form()
        updates = {}
        for field_name in SHARED_FIELDS:
            if field_name in ("item_id",):
                continue
            raw = form.get(field_name)
            if raw is None or raw == "":
                continue
            if field_name in ("price", "stock"):
                updates[field_name] = int(raw)
            elif field_name == "tags":
                updates[field_name] = [t.strip() for t in raw.split(",") if t.strip()]
            else:
                updates[field_name] = raw
        if updates:
            store.store_edit(item_key, updates, SHARED_FIELDS,
                             editor=form.get("editor") or "operator")
        return RedirectResponse(f"/records/{item_key}", status_code=303)

    @app.get("/conflicts", response_class=HTMLResponse)
    async def conflicts(request: Request):
        return templates.TemplateResponse(request=request, name="conflicts.html", context={
            "conflicts": store.list_conflicts(),
        })

    @app.post("/conflicts/{conflict_id}/resolve")
    async def resolve_conflict(conflict_id: int, action: str = Form(...)):
        store.resolve_conflict(conflict_id, action)
        return RedirectResponse("/conflicts", status_code=303)

    @app.get("/api/tasks/{task_id}")
    async def task_status(task_id: str):
        return store.get_task(task_id) or {}

    return app
