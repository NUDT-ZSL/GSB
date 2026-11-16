"""Deterministic local target site.

Serves a paginated catalog whose list items only appear after a scripted
second render (the crawler must poll the render endpoint until the item count
is stable), detail pages carrying a declared schemaVersion, and configurable
fault injection: 503s, slow responses, rate limiting (429) and session expiry.
"""
from __future__ import annotations

import asyncio
import json
import threading

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from ..config import SiteConfig
from .data import build_dataset, page_items

RENDER_JS = """// sample second-render script: fetches the render source and fills #items
(async function () {
  const box = document.getElementById('items');
  const src = box.getAttribute('data-render-src');
  const res = await fetch(src);
  const data = await res.json();
  box.innerHTML = data.items.map(i => `<a href="${i.href}">${i.item_id}</a>`).join('');
})();
"""

LIST_TEMPLATE = """<html><head><title>catalog page {page}</title></head><body>
<h1>内部资料目录 第 {page} 页</h1>
<div id="items" data-render-src="/render/list?page={page}" data-total-pages="{total}" data-next-page="{next_page}"></div>
<script src="/static/render.js"></script>
</body></html>"""

DETAIL_TEMPLATE = """<html><head><title>{item_id}</title>
<meta name="schemaVersion" content="{version}">
</head><body>
<h1>{item_id}</h1>
<script type="application/json" id="detail-data">{payload}</script>
</body></html>"""


class SiteState:
    def __init__(self, cfg: SiteConfig):
        self.reset(cfg)

    def reset(self, cfg: SiteConfig):
        self.cfg = cfg
        self.dataset = build_dataset(cfg)
        self.sessions = {}
        self.session_seq = 0
        self.inflight = 0
        self.render_seen = {}
        self.fail_503_remaining = dict(cfg.fail_503)
        self.request_counts = {"total": 0, "429": 0, "401": 0, "503": 0, "200": 0}


def create_app(cfg: SiteConfig) -> FastAPI:
    app = FastAPI(title="uid8-local-site")
    state = SiteState(cfg)
    app.state.site = state

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/__") or path == "/session" or path.startswith("/static"):
            return await call_next(request)
        state.request_counts["total"] += 1
        cfg_now = state.cfg
        if cfg_now.rate_limit_inflight and state.inflight >= cfg_now.rate_limit_inflight:
            state.request_counts["429"] += 1
            return JSONResponse({"error": "RATE_LIMITED"}, status_code=429,
                                headers={"Retry-After": "0"})
        token = request.headers.get("X-Session-Token")
        sess = state.sessions.get(token)
        if cfg_now.session_always_expired or sess is None or \
                (cfg_now.session_ttl and sess["used"] >= cfg_now.session_ttl):
            state.request_counts["401"] += 1
            return JSONResponse({"error": "SESSION_EXPIRED"}, status_code=401)
        sess["used"] += 1
        state.inflight += 1
        try:
            return await call_next(request)
        finally:
            state.inflight -= 1

    @app.post("/session")
    async def new_session():
        state.session_seq += 1
        token = f"tok-{state.session_seq}"
        state.sessions[token] = {"used": 0}
        return {"token": token}

    @app.get("/list")
    async def list_page(page: int = 1):
        total = state.cfg.pages
        next_page = str(page + 1) if page < total else ""
        return HTMLResponse(LIST_TEMPLATE.format(page=page, total=total, next_page=next_page))

    @app.get("/render/list")
    async def render_list(page: int = 1):
        seen = state.render_seen.get(page, 0)
        state.render_seen[page] = seen + 1
        if seen == 0:
            # script has not finished its first pass yet
            return {"pending": True, "items": []}
        items = [{"item_id": iid, "href": f"/detail/{iid}"} for iid in page_items(state.cfg, page)]
        return {"pending": False, "items": items}

    @app.get("/detail/{item_id}")
    async def detail(item_id: str):
        if state.fail_503_remaining.get(item_id, 0) > 0:
            state.fail_503_remaining[item_id] -= 1
            state.request_counts["503"] += 1
            return JSONResponse({"error": "BACKEND_UNAVAILABLE"}, status_code=503)
        delay_ms = state.cfg.slow_items.get(item_id, 0)
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000.0)
        rec = state.dataset.get(item_id)
        if rec is None:
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
        state.request_counts["200"] += 1
        html = DETAIL_TEMPLATE.format(
            item_id=item_id,
            version=rec["schemaVersion"],
            payload=json.dumps(rec["payload"], ensure_ascii=False),
        )
        return HTMLResponse(html)

    @app.get("/static/render.js")
    async def render_js():
        return PlainTextResponse(RENDER_JS, media_type="application/javascript")

    @app.post("/__config")
    async def update_config(request: Request):
        body = await request.json()
        for key, value in body.items():
            if hasattr(state.cfg, key):
                setattr(state.cfg, key, value)
        if "fail_503" in body:
            state.fail_503_remaining = dict(state.cfg.fail_503)
        state.dataset = build_dataset(state.cfg)
        return {"ok": True, "cfg": json.loads(state.cfg.to_json())}

    @app.post("/__reset")
    async def reset_state():
        state.reset(state.cfg)
        return {"ok": True}

    @app.get("/__stats")
    async def stats():
        return {"requests": dict(state.request_counts), "inflight": state.inflight}

    return app


def serve_in_thread(cfg: SiteConfig):
    """Start the site on cfg.host:cfg.port in a daemon thread. Returns (server, thread)."""
    import uvicorn

    config = uvicorn.Config(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.01)
    return server, thread
