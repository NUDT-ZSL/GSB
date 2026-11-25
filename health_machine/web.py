"""Optional local Flask facade for status and verification."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from .engine import process_pending
from .status import build_status
from .store import Store
from .verify import verify_store


def create_app(db_path: str | Path = "state/hsm.sqlite") -> Flask:
    app = Flask(__name__)
    app.config["HSM_DB"] = str(db_path)

    def store() -> Store:
        return Store(app.config["HSM_DB"])

    @app.get("/healthz")
    def healthz() -> Any:
        return jsonify({"ok": True, "service": "health-state-machine"})

    @app.get("/api/status")
    def api_status() -> Any:
        result = build_status(store(), request.args.get("instance_id"))
        return jsonify(result)

    @app.post("/api/probe")
    def api_probe() -> Any:
        instance_id = request.json.get("instance_id") if request.is_json else None
        result = process_pending(store(), instance_id)
        return jsonify({"ok": True, **result})

    @app.get("/api/verify")
    def api_verify() -> Any:
        window_index = request.args.get("window_index", type=int)
        result = verify_store(store(), request.args.get("instance_id"), window_index)
        return jsonify(result), 200 if result["ok"] else 409

    return app
