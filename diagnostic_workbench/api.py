from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .engine import Workbench
from .errors import BusinessError
from .util import canonical_json_bytes


def _json_response(value: Any, status_code: int = 200) -> Response:
    return Response(
        content=canonical_json_bytes(value),
        media_type="application/json",
        status_code=status_code,
    )


async def _read_json(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            code="INVALID_REQUEST",
            message=f"Malformed JSON body: {exc.msg}",
            field_path="$",
            context={"line": exc.lineno, "column": exc.colno},
            http_status=400,
        ) from exc


def create_app(state_dir: str | Path | None = None) -> FastAPI:
    resolved_state_dir = Path(
        state_dir
        or os.environ.get("WORKBENCH_STATE_DIR")
        or Path.cwd() / "state"
    )
    workbench = Workbench(resolved_state_dir)
    app = FastAPI(
        title="Local Failover Diagnostic Workbench",
        version="1.0.0",
        description=(
            "Deterministic logical-clock failover, isolation, query consistency, "
            "SQLite recovery and multi-process idempotency diagnostics."
        ),
    )
    app.state.workbench = workbench
    app.state.state_dir = resolved_state_dir

    @app.exception_handler(BusinessError)
    async def business_error_handler(
        request: Request,
        exc: BusinessError,
    ) -> Response:
        return _json_response(exc.to_dict(), status_code=exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> Response:
        error = BusinessError(
            code="INVALID_REQUEST",
            message="Request validation failed.",
            field_path="$",
            context={"errors": exc.errors()},
            http_status=400,
        )
        return _json_response(error.to_dict(), status_code=400)

    @app.exception_handler(Exception)
    async def internal_error_handler(
        request: Request,
        exc: Exception,
    ) -> Response:
        error = BusinessError(
            code="INTERNAL_ERROR",
            message=f"{type(exc).__name__}: {exc}",
            field_path=None,
            context={},
            http_status=500,
        )
        return _json_response(error.to_dict(), status_code=500)

    @app.get("/health")
    async def health() -> Response:
        return _json_response({"status": "ok"})

    @app.post("/load")
    async def load(request: Request) -> Response:
        payload = await _read_json(request)
        result = await run_in_threadpool(workbench.load, payload)
        return _json_response(result)

    @app.post("/switch")
    async def switch(request: Request) -> Response:
        payload = await _read_json(request)
        result = await run_in_threadpool(workbench.switch, payload)
        return _json_response(result)

    @app.post("/resume")
    async def resume(request: Request) -> Response:
        payload = await _read_json(request)
        result = await run_in_threadpool(workbench.resume, payload)
        return _json_response(result)

    @app.get("/status")
    async def status() -> Response:
        result = await run_in_threadpool(workbench.status)
        return _json_response(result)

    @app.post("/verify")
    async def verify(request: Request) -> Response:
        payload = await _read_json(request)
        result = await run_in_threadpool(workbench.verify, payload)
        return _json_response(result)

    @app.get("/query/{service_name}")
    async def query(service_name: str, pointer_version: int | None = None) -> Response:
        result = await run_in_threadpool(
            workbench.query,
            service_name,
            pointer_version=pointer_version,
        )
        return _json_response(result)

    @app.get("/ledger")
    async def ledger(round_id: int | None = None) -> Response:
        result = await run_in_threadpool(workbench.ledger, round_id)
        return _json_response(result)

    return app


app = create_app()

