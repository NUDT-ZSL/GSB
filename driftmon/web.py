"""Flask-based local interactive dashboard."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from .errors import E_CONFIG_INVALID, E_LINEAGE_BROKEN, E_RESOURCE_LIMIT, AppError
from .service import MonitoringService


def _json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def create_app(root: str | Path | None = None, initial_cursor: int = 2) -> Flask:
    app = Flask(__name__)
    app.json.sort_keys = True
    service = MonitoringService(
        root or os.environ.get("DRIFTMON_ROOT", "runtime/app"),
        initial_cursor=initial_cursor,
    )
    app.extensions["driftmon_service"] = service

    @app.get("/")
    def index() -> str:
        return render_template("dashboard.html")

    @app.get("/api/state")
    def api_state():
        return jsonify(service.dashboard_payload())

    @app.post("/api/reset")
    def api_reset():
        payload = _json_body()
        return jsonify(
            service.reset(clear=bool(payload.get("clear", True)))
        )

    @app.post("/api/advance")
    def api_advance():
        result = service.advance()
        return jsonify({"run": result.public(), "state": service.dashboard_payload()})

    @app.post("/api/window")
    def api_window():
        payload = _json_body()
        if "window_size" not in payload:
            raise AppError(
                E_CONFIG_INVALID,
                "缺少 window_size",
                field="window_size",
            )
        try:
            window_size = int(payload["window_size"])
        except (TypeError, ValueError) as exc:
            raise AppError(
                E_CONFIG_INVALID,
                "window_size 必须为整数",
                field="window_size",
            ) from exc
        result = service.set_window(window_size)
        return jsonify({"run": result.public(), "state": service.dashboard_payload()})

    @app.post("/api/stat-version/<feature_name>")
    def api_stat_version(feature_name: str):
        result = service.bump_stat_version(feature_name)
        return jsonify({"run": result.public(), "state": service.dashboard_payload()})

    @app.post("/api/features/<feature_name>/toggle")
    def api_toggle(feature_name: str):
        result = service.toggle_feature(feature_name)
        return jsonify({"run": result.public(), "state": service.dashboard_payload()})

    @app.post("/api/features/<feature_name>/redefine")
    def api_redefine(feature_name: str):
        payload = _json_body()
        kind = str(payload.get("kind", "category"))
        result = service.redefine_feature(feature_name, kind)
        return jsonify({"run": result.public(), "state": service.dashboard_payload()})

    @app.post("/api/demo/inject")
    def api_inject():
        payload = _json_body()
        result = service.inject_drift_and_advance(
            feature_name=str(payload.get("feature", "transaction_amount")),
            kind=str(payload.get("kind", "mean_shift")),
            magnitude=float(payload.get("magnitude", 3.5)),
        )
        return jsonify({"run": result.public(), "state": service.dashboard_payload()})

    @app.post("/api/selfcheck")
    def api_selfcheck():
        return jsonify({"selfcheck": service.selfcheck(), "state": service.dashboard_payload()})

    @app.get("/api/cache/<key>")
    def api_cache(key: str):
        return jsonify(service.explain_cache(key))

    @app.get("/api/alerts/<alert_id>/trace")
    def api_trace(alert_id: str):
        return jsonify({"trace": service.trace_alert(alert_id)})

    @app.errorhandler(AppError)
    def handle_app_error(error: AppError):
        status = 400
        if error.code == E_RESOURCE_LIMIT:
            status = 422
        elif error.code == E_LINEAGE_BROKEN:
            status = 409
        return jsonify({"error": error.to_dict()}), status

    return app

