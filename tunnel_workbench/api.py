from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from .config import (
    WorkbenchConfig,
    load_config,
    parse_config,
    parse_profile_update,
)
from .diagnostics import verify_state
from .dashboard import DASHBOARD_HTML
from .engine import ForwardEngine, install_test_kill_hook
from .errors import BusinessError, fail
from .storage import Storage


def _body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        fail("E_REQUEST_BODY", "request body must be a JSON object")
    return body


def _positive_rounds(value: Any, default: int = 1) -> int:
    if value is None:
        return default
    if type(value) is not int or value < 0:
        fail("E_ROUNDS_INVALID", "rounds must be a non-negative integer")
    return value


def create_app(
    *,
    db_path: str | Path,
    config: WorkbenchConfig | None = None,
    config_path: str | Path | None = None,
) -> Flask:
    if config is None and config_path is not None:
        config = load_config(config_path)
    storage = Storage(db_path)
    app = Flask(__name__)
    app.json.sort_keys = True
    app.extensions["tunnel_storage"] = storage
    app.extensions["tunnel_config"] = config
    app.extensions["tunnel_abort_events"] = {}

    @app.errorhandler(BusinessError)
    def business_error(exc: BusinessError):
        return jsonify({"ok": False, "error": exc.as_dict()}), exc.http_status

    @app.errorhandler(Exception)
    def internal_error(exc: Exception):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "details": {},
                    },
                }
            ),
            500,
        )

    def require_config() -> WorkbenchConfig:
        current = app.extensions.get("tunnel_config")
        if current is None:
            fail("E_CONFIG_NOT_LOADED", "load a configuration before forwarding")
        return current

    def require_storage() -> Storage:
        if not storage.has_schema():
            fail("E_CONFIG_NOT_LOADED", "load a configuration before using storage")
        return storage

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "service": "tunnel-workbench"})

    @app.get("/")
    def dashboard():
        return Response(DASHBOARD_HTML, mimetype="text/html")

    @app.post("/api/load")
    def load():
        body = _body()
        if "path" in body:
            loaded = load_config(body["path"])
        elif "config" in body:
            loaded = parse_config(body["config"], source="<http-request>")
        else:
            fail("E_REQUEST_BODY", "load requires path or config")
        installed = storage.install_config(loaded)
        app.extensions["tunnel_config"] = loaded
        return jsonify(
            {
                "ok": True,
                "config_digest": loaded.content_digest,
                "installed": installed["installed"],
            }
        )

    @app.get("/api/status")
    def status():
        return jsonify(require_storage().status())

    @app.post("/api/forward")
    def forward():
        loaded = require_config()
        body = _body()
        tunnel = body.get("tunnel")
        if not isinstance(tunnel, str) or not tunnel:
            fail("E_REQUEST_BODY", "forward requires a tunnel string")
        payload_index = body.get("payload_index", 0)
        if type(payload_index) is not int or payload_index < 0:
            fail("E_PAYLOAD_INDEX", "payload_index must be a non-negative integer")
        rounds = _positive_rounds(body.get("rounds", 1))
        abort_event = threading.Event()
        app.extensions["tunnel_abort_events"][tunnel] = abort_event
        engine = ForwardEngine(
            require_storage(),
            loaded,
            abort_event=abort_event,
            kill_hook=install_test_kill_hook(),
        )
        try:
            result = engine.run_new(
                tunnel=tunnel,
                payload_index=payload_index,
                rounds=rounds,
                transport_override=body.get("transport"),
                salt=body.get("salt", 0),
            )
        finally:
            app.extensions["tunnel_abort_events"].pop(tunnel, None)
        return jsonify(result)

    @app.post("/api/resume")
    def resume():
        loaded = require_config()
        body = _body()
        tunnel = body.get("tunnel")
        if not isinstance(tunnel, str) or not tunnel:
            fail("E_REQUEST_BODY", "resume requires a tunnel string")
        rounds = _positive_rounds(body.get("rounds", 1))
        abort_event = threading.Event()
        app.extensions["tunnel_abort_events"][tunnel] = abort_event
        engine = ForwardEngine(
            require_storage(),
            loaded,
            abort_event=abort_event,
            kill_hook=install_test_kill_hook(),
        )
        try:
            result = engine.resume(
                tunnel=tunnel,
                rounds=rounds,
                transport_override=body.get("transport"),
            )
        finally:
            app.extensions["tunnel_abort_events"].pop(tunnel, None)
        return jsonify(result)

    @app.post("/api/abort")
    def abort():
        database = require_storage()
        body = _body()
        if "run_id" in body:
            run = database.get_run(body["run_id"])
        elif "tunnel" in body:
            run = database.latest_resumable_run(body["tunnel"])
        else:
            fail("E_REQUEST_BODY", "abort requires run_id or tunnel")
        event = app.extensions["tunnel_abort_events"].get(run["tunnel"])
        if event is not None:
            event.set()
        clock = body.get("clock_ms", run["logical_clock_ms"])
        if type(clock) is not int or clock < 0:
            fail("E_CLOCK_INVALID", "clock_ms must be a non-negative integer")
        aborted = database.abort_run(run["run_id"], clock)
        return jsonify({"ok": True, "run": aborted})

    @app.get("/api/verify")
    def verify():
        loaded = app.extensions.get("tunnel_config")
        return jsonify(verify_state(require_storage(), loaded))

    @app.get("/api/profiles/<tunnel>")
    def profiles(tunnel: str):
        return jsonify(
            {
                "ok": True,
                "tunnel": tunnel,
                "profiles": require_storage().profiles_for_tunnel(tunnel),
            }
        )

    @app.post("/api/profiles/<tunnel>")
    def replace_profile(tunnel: str):
        body = _body()
        profile = parse_profile_update(body, tunnel)
        return jsonify({"ok": True, "profile": require_storage().replace_profile(profile)})

    @app.get("/api/runs/<run_id>")
    def run_status(run_id: str):
        return jsonify({"ok": True, "run": require_storage().get_run(run_id)})

    return app

