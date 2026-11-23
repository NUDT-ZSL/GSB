from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, Response, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .db import WorkbenchDB
from .engine import resume_checks, run_check
from .errors import WorkbenchError
from .store import (
    get_merge_view,
    get_status,
    list_rounds,
    list_snapshots,
    load_package,
    verify_state,
)
from .util import pretty_json


def stable_json(payload: Any, status: int = 200) -> Response:
    return Response(
        pretty_json(payload),
        status=status,
        mimetype="application/json",
    )


def create_app(workdir: str | Path) -> Flask:
    app = Flask(__name__, static_folder=None)
    db = WorkbenchDB(workdir)
    db.initialize()
    web_root = Path(__file__).parent / "web"

    def body() -> dict[str, Any]:
        if not request.is_json:
            return {}
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) else {}

    @app.errorhandler(WorkbenchError)
    def handle_workbench_error(exc: WorkbenchError) -> Response:
        return stable_json(exc.to_dict(), status=exc.http_status)

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException) -> Response:
        error = WorkbenchError(
            "E_HTTP",
            exc.description or exc.name,
            {"http_status": exc.code},
            http_status=exc.code or 500,
        )
        return stable_json(error.to_dict(), status=exc.code or 500)

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception) -> Response:
        app.logger.exception("unhandled workbench error")
        error = WorkbenchError(
            "E_INTERNAL",
            "unexpected internal error",
            {"exception_type": type(exc).__name__},
            http_status=500,
        )
        return stable_json(error.to_dict(), status=500)

    @app.get("/")
    def index() -> Response:
        return send_from_directory(web_root, "index.html")

    @app.get("/static/<path:filename>")
    def static_file(filename: str) -> Response:
        return send_from_directory(web_root, filename)

    @app.get("/health")
    def health() -> Response:
        return stable_json({"ok": True, "schema": "lb-health/v1"})

    @app.get("/api/status")
    def api_status() -> Response:
        return stable_json(get_status(db))

    @app.get("/api/merge")
    def api_merge() -> Response:
        return stable_json(get_merge_view(db))

    @app.get("/api/verify")
    def api_verify() -> Response:
        result = verify_state(db)
        return stable_json(result, status=200 if result["ok"] else 409)

    @app.get("/api/snapshots")
    def api_snapshots() -> Response:
        return stable_json(list_snapshots(db))

    @app.get("/api/rounds")
    def api_rounds() -> Response:
        return stable_json(list_rounds(db))

    @app.post("/api/load")
    def api_load() -> Response:
        payload = body()
        package_path = payload.get("package_path") or payload.get("package")
        if not isinstance(package_path, str) or not package_path:
            raise WorkbenchError(
                "E_BAD_REQUEST",
                "package_path is required",
                {"field": "package_path"},
            )
        result = load_package(db, package_path)
        return stable_json(result)

    @app.post("/api/check")
    def api_check() -> Response:
        payload = body()
        plan_path = payload.get("plan_path") or payload.get("plan")
        if not isinstance(plan_path, str) or not plan_path:
            raise WorkbenchError(
                "E_BAD_REQUEST",
                "plan_path is required",
                {"field": "plan_path"},
            )
        round_index = payload.get("round_index")
        if round_index is not None and (
            isinstance(round_index, bool) or not isinstance(round_index, int)
        ):
            raise WorkbenchError(
                "E_BAD_REQUEST",
                "round_index must be an integer",
                {"field": "round_index"},
            )
        instances = payload.get("instances")
        if instances is not None:
            if not isinstance(instances, list) or not all(
                isinstance(item, str) for item in instances
            ):
                raise WorkbenchError(
                    "E_BAD_REQUEST",
                    "instances must be an array of strings",
                    {"field": "instances"},
                )
        result = run_check(
            db,
            plan_path=plan_path,
            probe_base_url=(
                payload.get("probe_base_url")
                if isinstance(payload.get("probe_base_url"), str)
                else None
            ),
            round_index=round_index,
            instances=instances,
        )
        return stable_json(result)

    @app.post("/api/resume")
    def api_resume() -> Response:
        payload = body()
        base_url = payload.get("probe_base_url")
        result = resume_checks(
            db,
            probe_base_url=base_url if isinstance(base_url, str) else None,
        )
        return stable_json(result)

    return app
