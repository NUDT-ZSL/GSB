from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from pipeline.alignment import alignment_report
from pipeline.config import TaskConfig
from pipeline.db import Database
from pipeline.scheduler import run_task

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Catalog Pipeline Console</title>
<style>
body{font-family:system-ui,sans-serif;margin:2rem;max-width:960px}
fieldset{margin-bottom:1rem} label{display:block;margin:.25rem 0}
pre{background:#f4f4f4;padding:1rem;overflow:auto}
button{margin:.2rem .4rem .2rem 0}
</style></head><body>
<h1>Catalog Pipeline Console</h1>
<fieldset><legend>Create task</legend>
<label>task_id <input id="tid" value="main"></label>
<label>sources <input id="sources" value="alpha,beta"></label>
<label>concurrency <input id="conc" type="number" value="4"></label>
<label>retry_wait_budget <input id="rwb" type="number" value="8"></label>
<button onclick="createTask()">Create &amp; run</button>
</fieldset>
<fieldset><legend>Tasks</legend>
<button onclick="loadTasks()">Refresh</button>
<button onclick="resumeTask()">Resume selected task_id</button>
<pre id="tasks">{}</pre></fieldset>
<fieldset><legend>Failures of task</legend>
<button onclick="loadFailures()">Load failures</button>
<pre id="failures">{}</pre></fieldset>
<fieldset><legend>Field alignment</legend>
<label>business id <input id="biz" value="BIZ-001"></label>
<button onclick="loadAlignment()">Alignment report</button>
<pre id="align">{}</pre></fieldset>
<fieldset><legend>Site fault / version injection</legend>
<label>JSON <input id="cfg" size="60"
 value='{"faults":{"always_503":["beta/BIZ-004"]}}'></label>
<button onclick="pushConfig()">POST /__config</button>
<pre id="cfgout">{}</pre></fieldset>
<script>
const J = async (u, opt) => await (await fetch(u, opt)).json();
const show = (id, v) => document.getElementById(id).textContent = JSON.stringify(v, null, 2);
async function createTask(){
  const body = {task_id: tid.value, sources: sources.value.split(','),
    concurrency: +conc.value, retry_wait_budget: +rwb.value};
  show('tasks', await J('/api/tasks', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)}));
}
async function loadTasks(){ show('tasks', await J('/api/tasks')); }
async function resumeTask(){ show('tasks', await J('/api/tasks/'+tid.value+'/resume', {method:'POST'})); }
async function loadFailures(){ show('failures', await J('/api/tasks/'+tid.value+'/failures')); }
async function loadAlignment(){ show('align', await J('/api/alignment/'+biz.value)); }
async function pushConfig(){ show('cfgout', await J('/api/site/config', {method:'POST',
  headers:{'Content-Type':'application/json'}, body: cfg.value})); }
loadTasks();
</script></body></html>"""


def create_console_app(site_base_url: str, db_path: str) -> FastAPI:
    app = FastAPI(title="catalog-pipeline-console")
    db = Database(db_path)
    running: dict[str, asyncio.Task] = {}

    async def _run(cfg: TaskConfig) -> dict:
        async with httpx.AsyncClient(base_url=site_base_url) as client:
            return await run_task(cfg, db, client)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return INDEX_HTML

    @app.post("/api/tasks")
    async def create_task(request: Request) -> JSONResponse:
        body = await request.json()
        cfg = TaskConfig.from_dict(body)
        if cfg.task_id in running and not running[cfg.task_id].done():
            return JSONResponse({"error": "task already running"}, status_code=409)
        running[cfg.task_id] = asyncio.ensure_future(_run(cfg))
        return JSONResponse({"task_id": cfg.task_id, "state": "started"})

    @app.get("/api/tasks")
    async def list_tasks() -> list:
        return db.list_tasks()

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> JSONResponse:
        task = db.get_task(task_id)
        if task is None:
            return JSONResponse({"error": "unknown task"}, status_code=404)
        if task_id in running and not running[task_id].done():
            task["live"] = "running"
        return JSONResponse(task)

    @app.get("/api/tasks/{task_id}/failures")
    async def get_failures(task_id: str) -> list:
        return [i for i in db.task_items(task_id) if i["status"] == "failed"]

    @app.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: str) -> JSONResponse:
        task = db.get_task(task_id)
        if task is None:
            return JSONResponse({"error": "unknown task"}, status_code=404)
        cfg_data = (task.get("summary") or {}).get("config")
        if cfg_data:
            cfg = TaskConfig.from_dict(cfg_data)
        else:
            cfg = TaskConfig(task_id=task_id, sources=["alpha", "beta"])
        cfg.task_id = task_id
        if task_id in running and not running[task_id].done():
            return JSONResponse({"error": "task already running"}, status_code=409)
        running[task_id] = asyncio.ensure_future(_run(cfg))
        return JSONResponse({"task_id": task_id, "state": "resumed"})

    @app.get("/api/alignment/{biz_id}")
    async def get_alignment(biz_id: str) -> dict:
        return alignment_report(db, biz_id)

    @app.get("/api/site/config")
    async def get_site_config() -> dict:
        async with httpx.AsyncClient(base_url=site_base_url) as client:
            return (await client.get("/__config")).json()

    @app.post("/api/site/config")
    async def post_site_config(request: Request) -> JSONResponse:
        body = await request.json()
        async with httpx.AsyncClient(base_url=site_base_url) as client:
            resp = await client.post("/__config", json=body)
        return JSONResponse(resp.json())

    return app
