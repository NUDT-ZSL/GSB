"""FastAPI diagnostics interface (req 8).

Endpoints return stable-sorted JSON.  Business errors surface as
HTTP 422 with the error code and diagnosable fields preserved.

Run:  CWB_WORKDIR=run CWB_SAMPLE_DIR=samples \
        python -m uvicorn coord_workbench.api:app
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .engine import (
    Samples,
    build_state_report,
    form_batches,
    run_scheduler,
    verify_replay_consistency,
)
from .errors import BizError
from .invalidation import commit_policy
from .persistence import Ledger
from .recovery import recover


def create_app(workdir: Optional[str] = None, sample_dir: Optional[str] = None) -> FastAPI:
    workdir = workdir or os.environ.get("CWB_WORKDIR", "run")
    sample_dir = sample_dir or os.environ.get("CWB_SAMPLE_DIR", "samples")
    Path(workdir).mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="coord-workbench", version="0.1.0")
    app.state.ledger = Ledger(workdir)
    app.state.samples = Samples(sample_dir)
    app.state.last_former = None
    app.state.last_schedule = None

    @app.exception_handler(BizError)
    async def biz_error_handler(_: Request, exc: BizError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": exc.to_dict()})

    @app.get("/api/state")
    def get_state() -> Dict[str, Any]:
        return build_state_report(app.state.ledger, app.state.samples, app.state.last_former)

    @app.get("/api/topology")
    def get_topology() -> Dict[str, Any]:
        return app.state.samples.topo_index

    @app.post("/api/merge")
    def post_merge(max_concurrency: int = 16, time_budget_ms: int = 100_000) -> Dict[str, Any]:
        former, batches = form_batches(app.state.samples)
        app.state.last_former = former
        records = run_scheduler(
            app.state.ledger, batches, app.state.samples.nodes_by_name, max_concurrency, time_budget_ms
        )
        app.state.last_schedule = records
        return {"schedule": records, "state": build_state_report(app.state.ledger, app.state.samples, former)}

    @app.post("/api/recover")
    def post_recover() -> Dict[str, Any]:
        _, batches = form_batches(app.state.samples)
        report = recover(app.state.ledger, batches, app.state.samples.nodes_by_name)
        return {"recovery": report, "state": build_state_report(app.state.ledger, app.state.samples)}

    @app.get("/api/verify")
    def get_verify() -> Dict[str, Any]:
        return verify_replay_consistency(app.state.samples)

    @app.post("/api/policy/commit")
    def post_policy(payload: Dict[str, Any]) -> Dict[str, Any]:
        return commit_policy(
            app.state.ledger,
            version=int(payload["version"]),
            snapshot=payload.get("snapshot", {}),
            changed_nodes=list(payload.get("changed_nodes", [])),
            topo_views=app.state.samples.topo_index["views"],
            subscriptions=app.state.samples.subs,
            clock=int(payload.get("clock_ms", 0)),
        )

    @app.get("/api/policy/status")
    def get_policy_status() -> Dict[str, Any]:
        return {
            "current_version": app.state.ledger.current_policy_version(),
            "callback_log": app.state.ledger.callback_log(),
        }

    return app


app = create_app()
