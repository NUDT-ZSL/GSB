from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Flask, Response, request, render_template

from .errors import AlertLabError, invalid
from .service import AlertLabService
from .util import pretty_json, read_json


def json_response(payload: Any, status: int = 200) -> Response:
    return Response(
        pretty_json(payload) + "\n",
        status=status,
        mimetype="application/json",
    )


def _body() -> dict[str, Any]:
    if request.is_json:
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) else {}
    return {key: value for key, value in request.form.items()}


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise invalid("E_BAD_REQUEST", "logical clock must be an integer")


def create_app(
    workspace: str | Path | None = None,
    *,
    database_path: str | Path | None = None,
) -> Flask:
    root = Path(workspace).resolve() if workspace is not None else Path.cwd().resolve()
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["ALERTLAB_WORKSPACE"] = str(root)
    service = AlertLabService(root, database_path=database_path)
    app.extensions["alertlab"] = service

    @app.errorhandler(AlertLabError)
    def handle_alertlab_error(error: AlertLabError) -> Response:
        return json_response(error.to_dict(), status=error.http_status)

    @app.errorhandler(404)
    def handle_not_found(_: Any) -> Response:
        return json_response(
            {
                "ok": False,
                "error": {"code": "E_NOT_FOUND", "message": "route not found"},
            },
            status=404,
        )

    @app.errorhandler(Exception)
    def handle_unexpected(error: Exception) -> Response:
        if app.config.get("TESTING"):
            raise error
        return json_response(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERNAL",
                    "message": "unexpected local processing error",
                    "details": {"type": type(error).__name__},
                },
            },
            status=500,
        )

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/health")
    def health() -> Response:
        return json_response(
            {
                "ok": True,
                "component": "alertlab",
                "bind": "127.0.0.1",
                "workspace": str(root),
            }
        )

    @app.post("/load")
    def load() -> Response:
        body = _body()
        result = app.extensions["alertlab"].load(
            v1_path=body.get("v1_path") or None,
            v2_path=body.get("v2_path") or None,
            topology_path=body.get("topology_path") or None,
            events_path=body.get("events_path") or None,
            logical_clock_ms=_optional_int(body.get("logical_clock_ms")),
            include_v1=body.get("include_v1", True) not in (False, "false", "0", 0),
            include_v2=body.get("include_v2", True) not in (False, "false", "0", 0),
        )
        return json_response(result)

    @app.post("/patch")
    @app.get("/patch")
    def patch() -> Response:
        body = _body()
        patch_file = body.get("patch_file")
        if patch_file:
            path = Path(str(patch_file))
            if not path.is_absolute():
                path = root / path
            try:
                body = read_json(path)
            except FileNotFoundError:
                raise invalid("E_NOT_FOUND", "patch file does not exist", path=str(path), http_status=404)
            except (OSError, ValueError) as exc:
                raise invalid("E_PATCH_SCHEMA", "patch file is not valid JSON", error=str(exc))
        result = app.extensions["alertlab"].patch(body)
        return json_response(result)

    @app.get("/status")
    @app.post("/status")
    def status() -> Response:
        return json_response(app.extensions["alertlab"].status())

    @app.post("/verify")
    @app.get("/verify")
    def verify() -> Response:
        result = app.extensions["alertlab"].verify()
        return json_response(result, status=200 if result["ok"] else 409)

    @app.post("/reset")
    def reset() -> Response:
        service.store.reset()
        return json_response({"ok": True, "reset": True, "status": service.status()})

    return app
