from __future__ import annotations

from typing import Any

from flask import Flask, Response, render_template, request

from .engine import Autoscaler
from .errors import BusinessError
from .models import ScalerSpec, canonical_json
from .storage import SQLiteStore


def create_app(
    *,
    store: SQLiteStore,
    autoscaler: Autoscaler,
    spec: ScalerSpec,
    spec_paths: dict[str, str] | None = None,
) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.json.sort_keys = True

    def payload() -> dict[str, Any]:
        state, snapshot_id = store.load()
        status = autoscaler.status_dict(state)
        status["snapshot_id"] = snapshot_id
        status["events"] = store.read_events(limit=100)
        status["recovery_audit"] = store.read_recovery_audit(limit=20)
        status["snapshot_versions"] = store.snapshot_versions(limit=20)
        status["spec_paths"] = spec_paths or {}
        return status

    @app.get("/")
    def index() -> str:
        return render_template("workbench.html")

    @app.get("/api/status")
    def api_status() -> Response:
        return Response(canonical_json(payload()), mimetype="application/json")

    @app.get("/api/health")
    def api_health() -> Response:
        state, snapshot_id = store.load()
        return Response(
            canonical_json(
                {
                    "ok": True,
                    "snapshot_id": snapshot_id,
                    "round_watermark": state["round_watermark"],
                    "pending_rounds": len(state["pending_rounds"]),
                }
            ),
            mimetype="application/json",
        )

    @app.post("/api/run")
    def api_run() -> Response:
        request_body = request.get_json(silent=True) or {}
        if not isinstance(request_body, dict):
            return error_response(
                BusinessError("E_INPUT_INVALID", "request body must be a JSON object")
            )
        try:
            rounds = int(request_body.get("rounds", 1))
        except (TypeError, ValueError):
            return error_response(
                BusinessError("E_INPUT_INVALID", "rounds must be an integer")
            )
        start_clock = request_body.get("start_clock_ms")
        services = request_body.get("services")
        request_id = request_body.get("request_id")
        if services is not None and not isinstance(services, list):
            return error_response(
                BusinessError("E_INPUT_INVALID", "services must be a list")
            )
        if services is not None and not all(
            isinstance(name, str) for name in services
        ):
            return error_response(
                BusinessError("E_INPUT_INVALID", "services must contain strings")
            )
        if request_id is not None and not isinstance(request_id, str):
            return error_response(
                BusinessError("E_INPUT_INVALID", "request_id must be a string")
            )
        try:
            start_clock_value = int(start_clock) if start_clock is not None else None
        except (TypeError, ValueError):
            return error_response(
                BusinessError("E_INPUT_INVALID", "start_clock_ms must be an integer")
            )
        result = autoscaler.run(
            rounds=rounds,
            start_clock_ms=start_clock_value,
            services=services,
            request_id=request_id,
        )
        return Response(canonical_json(result), mimetype="application/json")

    @app.post("/api/recover")
    def api_recover() -> Response:
        result = autoscaler.recover()
        return Response(canonical_json(result), mimetype="application/json")

    @app.get("/api/events")
    def api_events() -> Response:
        return Response(
            canonical_json(
                {
                    "ok": True,
                    "events": store.read_events(limit=200),
                    "recovery_audit": store.read_recovery_audit(limit=50),
                    "snapshot_versions": store.snapshot_versions(limit=50),
                }
            ),
            mimetype="application/json",
        )

    @app.get("/api/config")
    def api_config() -> Response:
        state, _ = store.load()
        return Response(
            canonical_json({"ok": True, "spec": state["spec"]}),
            mimetype="application/json",
        )

    @app.errorhandler(BusinessError)
    def handle_business_error(exc: BusinessError) -> Response:
        return error_response(exc)

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception) -> Response:
        if isinstance(exc, BusinessError):
            return error_response(exc)
        wrapped = BusinessError(
            "E_STORAGE_ERROR",
            f"unhandled workbench failure: {type(exc).__name__}: {exc}",
        )
        return error_response(wrapped, status=500)

    return app


def error_response(exc: BusinessError, status: int = 400) -> Response:
    mapping = {
        "E_STATE_VERSION_CONFLICT": 409,
        "E_CONFIG_VERSION_REGRESSION": 409,
        "E_CONFIG_VERSION_CONFLICT": 409,
        "E_REQUEST_CONFLICT": 409,
        "E_REQUEST_IN_PROGRESS": 409,
        "E_LEDGER_MISMATCH": 500,
        "E_STORAGE_ERROR": 500,
    }
    return Response(
        canonical_json(exc.to_dict()),
        status=mapping.get(exc.code, status),
        mimetype="application/json",
    )
