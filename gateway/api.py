from __future__ import annotations

import json
import os
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from .engine import GatewayEngine
from .errors import E_BAD_SAMPLE, GatewayError


app = FastAPI(
    title="Ordered Delivery Gateway",
    version="1.0.0",
    description="SQLite-backed deterministic ordered-delivery control/data plane",
)


def _engine() -> GatewayEngine:
    return GatewayEngine(os.environ.get("GATEWAY_DB", "data/gateway.db"))


def _json_response(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=json.loads(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
        ),
        status_code=status_code,
    )


def _stable_response(payload: Any, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        media_type="application/json",
        status_code=status_code,
    )


@app.exception_handler(GatewayError)
async def _gateway_error_handler(
    request: Request,
    exc: GatewayError,
) -> JSONResponse:
    del request
    return _json_response(exc.to_dict(), status_code=exc.http_status or 400)


@app.exception_handler(Exception)
async def _internal_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    del request
    return _json_response(
        {
            "ok": False,
            "error": {
                "code": "E_INTERNAL",
                "message": str(exc),
                "details": {"type": type(exc).__name__},
            },
        },
        status_code=500,
    )


@app.get("/health")
def health() -> Response:
    return _stable_response({"ok": True, "service": "ordered-delivery-gateway"})


@app.post("/load")
def load(
    payload: dict[str, Any] = Body(...),
    ingest_events: bool = Query(default=True),
) -> Response:
    required = {"topics", "events", "subscriptions", "config"}
    missing = sorted(required - set(payload))
    if missing:
        raise GatewayError(
            E_BAD_SAMPLE,
            "load payload is missing document fields",
            {"missing": missing},
        )
    result = _engine().load_documents(
        payload["topics"],
        payload["events"],
        payload["subscriptions"],
        payload["config"],
        ingest_events=ingest_events,
    )
    return _stable_response(result)


@app.post("/produce")
def produce(payload: Any = Body(...)) -> Response:
    return _stable_response(_engine().produce(payload))


@app.post("/deliver")
def deliver(payload: dict[str, Any] = Body(...)) -> Response:
    subscriber_id = payload.get("subscriber_id") or payload.get("subscriber")
    if not subscriber_id:
        raise GatewayError(
            E_BAD_SAMPLE,
            "deliver payload requires subscriber_id",
            {"field": "subscriber_id"},
        )
    return _stable_response(
        _engine().deliver(
            str(subscriber_id),
            round_id=payload.get("round_id"),
            budget_override_ms=payload.get("budget_ms"),
        )
    )


@app.post("/resume")
def resume(payload: dict[str, Any] | None = Body(default=None)) -> Response:
    payload = payload or {}
    return _stable_response(
        _engine().resume(
            subscriber_id=payload.get("subscriber_id") or payload.get("subscriber"),
            round_id=payload.get("round_id"),
            budget_override_ms=payload.get("budget_ms"),
        )
    )


@app.post("/confirm")
def confirm(payload: dict[str, Any] = Body(...)) -> Response:
    delivery_id_value = payload.get("delivery_id")
    subscriber_id = payload.get("subscriber_id") or payload.get("subscriber")
    if not delivery_id_value or not subscriber_id:
        raise GatewayError(
            E_BAD_SAMPLE,
            "confirm payload requires delivery_id and subscriber_id",
            {
                "fields": ["delivery_id", "subscriber_id"],
                "payload": payload,
            },
        )
    return _stable_response(
        _engine().confirm(
            str(delivery_id_value),
            subscriber_id=str(subscriber_id),
        )
    )


@app.get("/status")
def status() -> Response:
    return _stable_response(_engine().status())


@app.get("/verify")
def verify() -> Response:
    result = _engine().verify()
    return _stable_response(result, status_code=200 if result.get("ok") else 500)
