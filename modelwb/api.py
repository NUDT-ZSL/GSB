from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from .errors import WorkbenchError
from .workbench import Workbench


def create_app(db_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    resolved_db = db_path or os.environ.get("MODELWB_DB", "var/workbench.db")
    workbench = Workbench(resolved_db)
    app.extensions["modelwb"] = workbench

    @app.errorhandler(WorkbenchError)
    def handle_workbench_error(error: WorkbenchError):
        return jsonify(error.to_dict()), error.http_status

    @app.errorhandler(404)
    def handle_not_found(_error):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "NOT_FOUND",
                        "business_code": "MWB-1404",
                        "message": "route not found",
                        "diagnostics": {},
                    },
                }
            ),
            404,
        )

    @app.errorhandler(Exception)
    def handle_unexpected(error: Exception):
        if isinstance(error, HTTPException):
            return error
        app.logger.exception("unhandled workbench API error")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "IO_ERROR",
                        "business_code": "MWB-1500",
                        "message": "internal workbench error",
                        "diagnostics": {"type": type(error).__name__},
                    },
                }
            ),
            500,
        )

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "status": "healthy", "db": str(workbench.storage.path)})

    @app.get("/api/status")
    def status():
        return jsonify(workbench.status())

    @app.get("/api/versions")
    def versions():
        return jsonify(workbench.versions())

    @app.get("/api/caches")
    def caches():
        include_invalid = request.args.get("include_invalid", "true").lower() == "true"
        caches_value = workbench.storage.list_caches()
        if not include_invalid:
            caches_value = [item for item in caches_value if item["valid"]]
        return jsonify({"ok": True, "caches": caches_value})

    @app.post("/api/load")
    def load():
        body = _json_body()
        result = workbench.load(
            input_dir=body.get("input_dir"),
            v1_path=body.get("v1_path"),
            v2_db_path=body.get("v2_db_path"),
            v2_dir=body.get("v2_dir"),
            registries_path=body.get("registries_path"),
            replicas_path=body.get("replicas_path"),
            baseline_version=body.get("baseline_version"),
            op_id=body.get("op_id"),
            clock_tolerance=int(body.get("clock_tolerance", 50)),
        )
        return jsonify(result)

    @app.post("/api/patch")
    def patch():
        body = _json_body()
        result = workbench.patch(
            patch_path=body["patch_path"],
            full_path=body.get("full_path"),
            op_id=body.get("op_id"),
            clock_tolerance=int(body.get("clock_tolerance", 50)),
        )
        return jsonify(result)

    @app.post("/api/gray")
    def gray():
        body = _json_body()
        result = workbench.gray(
            target_version=body["target_version"],
            stages=body["stages"],
            run_id=body.get("run_id"),
            fail_replica=body.get("fail_replica"),
            op_id=body.get("op_id"),
        )
        return jsonify(result)

    @app.post("/api/rollback")
    def rollback():
        body = _json_body()
        result = workbench.rollback(
            run_id=body.get("run_id"),
            reason=body.get("reason", "failure"),
            trigger=body.get("trigger", "api"),
            window_ms=int(body.get("window_ms", 5000)),
            op_id=body.get("op_id"),
        )
        return jsonify(result)

    @app.post("/api/resume")
    def resume():
        body = _json_body()
        return jsonify(workbench.resume(body.get("op_id")))

    @app.post("/api/verify")
    def verify():
        body = _json_body()
        result = workbench.verify_patch(
            patch_path=body["patch_path"],
            full_path=body.get("full_path"),
            base_version=body.get("base_version"),
        )
        return jsonify(result)

    @app.post("/api/requests")
    def start_request():
        body = _json_body()
        return jsonify(workbench.start_request(body["request_id"]))

    @app.get("/api/requests/<request_id>")
    def get_request(request_id: str):
        return jsonify(workbench.storage.get_request(request_id))

    @app.post("/api/requests/<request_id>/finish")
    def finish_request(request_id: str):
        return jsonify(workbench.finish_request(request_id))

    @app.get("/api/operations/<op_id>")
    def get_operation(op_id: str):
        return jsonify(workbench.storage.get_operation(op_id))

    return app


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise WorkbenchError("VALIDATION_ERROR", "request body must be a JSON object")
    return body
