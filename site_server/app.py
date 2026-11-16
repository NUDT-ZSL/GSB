from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import render_pages, sample_data

DEFAULT_FAULTS = {
    "always_503": [],
    "flaky_503": {},
    "always_slow": [],
    "flaky_slow": {},
    "session_trap": [],
    "flaky_session": {},
}


@dataclass
class SiteState:
    faults: dict = field(default_factory=lambda: {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULT_FAULTS.items()})
    version_overrides: dict = field(default_factory=dict)
    slow_seconds: float = 1.0
    unstable_pages: list = field(default_factory=list)
    list_obs: dict = field(default_factory=dict)
    flaky_counts: dict = field(default_factory=dict)
    tokens: set = field(default_factory=set)
    token_seq: int = 0

    def reset_transient(self) -> None:
        self.list_obs = {}
        self.flaky_counts = {
            kind: dict(mapping)
            for kind, mapping in self.faults.items()
            if isinstance(mapping, dict)
        }

    def snapshot(self) -> dict:
        return {
            "faults": self.faults,
            "version_overrides": self.version_overrides,
            "slow_seconds": self.slow_seconds,
            "unstable_pages": self.unstable_pages,
        }


def _expired() -> JSONResponse:
    return JSONResponse(
        {"error": "session_expired"},
        status_code=401,
        headers={"X-Session-Expired": "1"},
    )


def create_app(state: SiteState | None = None) -> FastAPI:
    state = state or SiteState()
    state.reset_transient()
    app = FastAPI(title="catalog-site-sim")

    def _flaky_hit(key: str, kind: str) -> bool:
        remaining = state.flaky_counts.get(kind, {}).get(key, 0)
        if remaining > 0:
            state.flaky_counts[kind][key] = remaining - 1
            return True
        return False

    def _session_ok(request: Request) -> bool:
        return request.cookies.get("sid") in state.tokens

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/session")
    async def session() -> JSONResponse:
        state.token_seq += 1
        token = f"tok-{state.token_seq}"
        state.tokens.add(token)
        resp = JSONResponse({"token": token})
        resp.set_cookie("sid", token)
        return resp

    @app.get("/list/{source}")
    async def list_page(source: str, page: int = 1, request: Request = None):
        if not _session_ok(request):
            return _expired()
        obs_key = f"{source}/{page}"
        state.list_obs[obs_key] = state.list_obs.get(obs_key, 0) + 1
        obs = state.list_obs[obs_key]
        if obs < 2:
            # Simulates the pre-render pass: the JS-driven item container
            # has not been mounted yet.
            return HTMLResponse(render_pages.render_list_loading())
        items = sample_data.page_slice(page)
        if obs_key in state.unstable_pages and obs % 2 == 1:
            items = items + ["BIZ-999"]
        total = sample_data.total_pages()
        next_page = str(page + 1) if page < total else ""
        return HTMLResponse(render_pages.render_list(source, items, total, next_page))

    @app.get("/detail/{source}/{biz}")
    async def detail_page(source: str, biz: str, request: Request = None):
        key = f"{source}/{biz}"
        if not _session_ok(request):
            return _expired()
        if key in state.faults["session_trap"] or _flaky_hit(key, "flaky_session"):
            return _expired()
        if key in state.faults["always_503"] or _flaky_hit(key, "flaky_503"):
            return Response(status_code=503)
        if key in state.faults["always_slow"] or _flaky_hit(key, "flaky_slow"):
            await asyncio.sleep(state.slow_seconds)
        n = int(biz.rsplit("-", 1)[1])
        version = state.version_overrides.get(key, sample_data.default_version(source, n))
        return HTMLResponse(render_pages.render_detail(source, version, sample_data.product(biz)))

    @app.get("/__config")
    async def get_config() -> dict:
        return state.snapshot()

    @app.post("/__config")
    async def post_config(request: Request) -> dict:
        body = await request.json()
        if "faults" in body:
            merged = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULT_FAULTS.items()}
            for k, v in body["faults"].items():
                merged[k] = v
            state.faults = merged
        if "version_overrides" in body:
            state.version_overrides = dict(body["version_overrides"])
        if "slow_seconds" in body:
            state.slow_seconds = float(body["slow_seconds"])
        if "unstable_pages" in body:
            state.unstable_pages = list(body["unstable_pages"])
        state.reset_transient()
        return state.snapshot()

    @app.post("/__reset")
    async def reset() -> dict:
        state.reset_transient()
        return {"ok": True}

    return app
