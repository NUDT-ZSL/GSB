"""Minimal local Flask JSON API over the same engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from .engine import EventBus
from .errors import BusinessError
from .validation import load_dataset


def create_app(db_path: str | Path) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False
    bus = EventBus(db_path)

    @app.errorhandler(BusinessError)
    def handle_business_error(error: BusinessError):
        return jsonify(error.as_dict()), error.http_status

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "local-eventbus"})

    @app.get("/status")
    def status():
        return jsonify(bus.status())

    @app.get("/verify")
    def verify():
        result = bus.verify()
        return jsonify(result), 200 if result["ok"] else 409

    @app.post("/load")
    def load():
        body = _body()
        dataset = load_dataset(
            body["v1"],
            body["v2_shards"],
            body["v2_metadata"],
            retention_bytes=body.get("retention_bytes"),
            clock_tolerance_ms=body.get("clock_tolerance_ms"),
        )
        return jsonify(
            bus.load(
                dataset,
                replace=bool(body.get("replace", False)),
                clock_ms=body.get("clock_ms"),
            )
        )

    @app.post("/publish/<channel>")
    def publish(channel: str):
        body = _body()
        return jsonify(
            bus.publish_events(
                channel,
                body,
                max_batch=body.get("max_batch"),
                clock_ms=body.get("clock_ms"),
            )
        )

    @app.post("/edit")
    def edit():
        body = _body()
        return jsonify(bus.edit(body, clock_ms=body.get("clock_ms")))

    @app.post("/bridge/<bridge_id>")
    def bridge(bridge_id: str):
        body = _body(silent=True)
        return jsonify(
            bus.run_bridge(
                bridge_id,
                max_events=body.get("max_events"),
                clock_ms=body.get("clock_ms"),
            )
        )

    @app.post("/bridge/<bridge_id>/position")
    def report_bridge_position(bridge_id: str):
        body = _body()
        return jsonify(
            bus.report_bridge_position(
                bridge_id,
                body["to_offset"],
                expected_digest=body.get("expected_digest"),
                config_version=body.get("config_version"),
            )
        )

    @app.post("/consume/<subscription>")
    def consume(subscription: str):
        body = _body()
        return jsonify(
            bus.consume(
                subscription,
                count=body.get("count"),
                to_offset=body.get("to_offset"),
                request_id=body.get("request_id"),
                clock_ms=body.get("clock_ms"),
            )
        )

    @app.post("/resume")
    def resume():
        result = bus.resume()
        return jsonify(result), 200 if result["ok"] else 409

    @app.post("/cleanup")
    def cleanup():
        body = _body(silent=True)
        return jsonify(
            bus.cleanup(
                clock_ms=body.get("clock_ms"),
                retention_bytes=body.get("retention_bytes"),
            )
        )

    return app


def _body(silent: bool = False) -> dict[str, Any]:
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
    if silent:
        return {}
    raise BusinessError("PATCH_INVALID", "请求体必须为 JSON 对象")
