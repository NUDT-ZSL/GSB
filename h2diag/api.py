from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response

from .canonical import canonical_json, digest_json
from .errors import DiagnosticError, PATCH_JSON_001, SCENARIO_JSON_001, VERIFY_001, error
from .service import DiagnosticService
from .validation import normalize_scenario, validate_patch_document


def _stable_response(payload: dict[str, Any], status_code: int = 200) -> Response:
    return Response(
        content=canonical_json(payload),
        status_code=status_code,
        media_type="application/json",
    )


def create_app(db_path: str | Path, *, service: DiagnosticService | None = None) -> FastAPI:
    app = FastAPI(
        title="HTTP/2 Multiplexing Recovery Diagnostics",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
    )
    diagnostic = service or DiagnosticService(db_path)
    app.state.diagnostic_service = diagnostic

    @app.exception_handler(DiagnosticError)
    async def diagnostic_error_handler(
        request: Request, exc: DiagnosticError
    ) -> Response:
        return Response(
            status_code=exc.http_status,
            content=canonical_json({"ok": False, "error": exc.as_dict()}),
            media_type="application/json",
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> Response:
        if request.url.path.endswith("/scenario"):
            code = SCENARIO_JSON_001
        elif request.url.path.endswith("/patch"):
            code = PATCH_JSON_001
        else:
            code = VERIFY_001
        return Response(
            status_code=422,
            content=canonical_json(
                {
                    "ok": False,
                    "error": {
                        "code": code,
                        "message": "request JSON validation failed",
                        "category": "validation",
                        "details": {"errors": jsonable_encoder(exc.errors())},
                    },
                }
            ),
            media_type="application/json",
        )

    @app.get("/health")
    def health() -> Response:
        return _stable_response({"ok": True, "database": diagnostic.db.health()})

    @app.post("/validate/scenario")
    def validate_scenario_endpoint(payload: Any = Body(...)) -> Response:
        scenario = normalize_scenario(payload, preflight=True)
        return _stable_response(
            {
                "ok": True,
                "scenario_id": scenario["scenario_id"],
                "connection_id": scenario["connection_id"],
                "stream_count": len(scenario["streams"]),
                "event_count": len(scenario["events"]),
                "scenario_digest": digest_json(scenario),
            }
        )

    @app.post("/validate/patch")
    def validate_patch_endpoint(
        payload: Any = Body(...),
        connection_id: str | None = Query(default=None),
    ) -> Response:
        patch = validate_patch_document(payload)
        result: dict[str, Any] = {
            "ok": True,
            "patch_id": patch["patch_id"],
            "base_version": patch["base_version"],
            "target_version": patch["target_version"],
            "patch_digest": patch["_computed_digest"],
        }
        if connection_id is not None:
            scenario = diagnostic.db.get_scenario_document(connection_id)
            from .patching import apply_patch_in_memory

            state = diagnostic.db.get_state(connection_id)
            _, _, kind, manifest = apply_patch_in_memory(
                scenario,
                state,
                patch,
                committed_seq=int(state["committed_seq"]),
            )
            result["classification"] = kind
            result["manifest"] = manifest
        return _stable_response(result)

    @app.post("/run")
    def run_endpoint(payload: Any = Body(...)) -> Response:
        if not isinstance(payload, dict):
            raise error(SCENARIO_JSON_001, "run body must be a JSON object")
        scenario: dict[str, Any] | None
        if "scenario" in payload:
            scenario = payload["scenario"]
            options = payload
        elif payload.get("format") == "h2diag.scenario/v1":
            scenario = payload
            options = {}
        else:
            scenario = None
            options = payload
        connection_id = payload.get("connection_id")
        if scenario is not None:
            initialized = diagnostic.initialize(scenario)
            connection_id = initialized["connection_id"]
        if not connection_id:
            raise error(
                VERIFY_001,
                "connection_id is required when no scenario is supplied",
                details={},
            )
        max_frames = options.get("max_frames")
        until_clock = options.get("until_clock")
        result = diagnostic.run(
            connection_id,
            max_frames=max_frames,
            until_clock=until_clock,
            expected_patch_version=options.get("patch_version"),
        )
        result["ok"] = True
        return _stable_response(result)

    @app.post("/connections/{connection_id}/run")
    def run_connection_endpoint(
        connection_id: str, payload: dict[str, Any] = Body(default={})
    ) -> Response:
        result = diagnostic.run(
            connection_id,
            max_frames=payload.get("max_frames"),
            until_clock=payload.get("until_clock"),
            expected_patch_version=payload.get("patch_version"),
        )
        result["ok"] = True
        return _stable_response(result)

    @app.post("/patch")
    def patch_endpoint(payload: Any = Body(...)) -> Response:
        if not isinstance(payload, dict):
            raise error(PATCH_JSON_001, "patch body must be a JSON object")
        if "patch" in payload:
            patch_document = payload["patch"]
            connection_id = payload.get("connection_id") or patch_document.get("connection_id")
        else:
            patch_document = payload
            connection_id = payload.get("connection_id")
        if not connection_id:
            raise error(VERIFY_001, "connection_id is required for patch")
        result = diagnostic.apply_patch(connection_id, patch_document)
        result["ok"] = True
        return _stable_response(result)

    @app.post("/resume")
    def resume_endpoint(payload: dict[str, Any] = Body(...)) -> Response:
        connection_id = payload.get("connection_id")
        if not connection_id:
            raise error(VERIFY_001, "connection_id is required for resume")
        result = diagnostic.resume(
            connection_id,
            max_frames=payload.get("max_frames"),
            until_clock=payload.get("until_clock"),
        )
        result["ok"] = True
        return _stable_response(result)

    @app.get("/status")
    def status_endpoint(connection_id: str = Query(...)) -> Response:
        return _stable_response(diagnostic.status(connection_id))

    @app.post("/verify")
    def verify_endpoint(payload: dict[str, Any] = Body(...)) -> Response:
        connection_id = payload.get("connection_id")
        if not connection_id:
            raise error(VERIFY_001, "connection_id is required for verify")
        report = diagnostic.verify(connection_id)
        return _stable_response(report, 200 if report["ok"] else 409)

    @app.get("/verify")
    def verify_get_endpoint(connection_id: str = Query(...)) -> Response:
        report = diagnostic.verify(connection_id)
        return _stable_response(report, 200 if report["ok"] else 409)

    return app


app = create_app(os.environ.get("H2DIAG_DB", "artifacts/h2diag.db"))
