"""Loopback-only Flask API for patch submission and status queries."""

from __future__ import annotations

import ipaddress
import json
from typing import Any

from flask import Flask, Response, request

from .canonical import CanonicalJSONError, strict_json_loads
from .engine import ConfigHub
from .errors import ConfigHubError, bad_clock, bad_patch, not_found


def _response(payload: dict[str, Any], status: int = 200) -> Response:
    body = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return Response(body + "\n", status=status, content_type="application/json")


def create_app(hub: ConfigHub) -> Flask:
    app = Flask(__name__)
    app.json.sort_keys = True

    @app.errorhandler(ConfigHubError)
    def handle_business_error(error: ConfigHubError) -> Response:
        return _response(error.to_dict(), error.http_status)

    @app.errorhandler(404)
    def handle_not_found(_error: Any) -> Response:
        return _response(
            {
                "ok": False,
                "error_code": "E_NOT_FOUND",
                "message": "HTTP endpoint does not exist",
            },
            404,
        )

    @app.get("/health")
    def health() -> Response:
        return _response({"ok": True, "service": "config-hub", "loopback_only": True})

    @app.get("/v1/status")
    def status() -> Response:
        return _response(hub.status())

    @app.get("/v1/config")
    def config() -> Response:
        version_value = request.args.get("version")
        if version_value is None:
            return _response({"ok": True, "config": hub.read_config()})
        try:
            version = int(version_value)
        except ValueError as exc:
            raise not_found(f"invalid configuration version: {version_value}") from exc
        return _response({"ok": True, "config": hub.read_config(version)})

    @app.post("/v1/patches")
    @app.post("/v1/apply")
    def apply_patch() -> Response:
        try:
            body = strict_json_loads(request.get_data(as_text=True))
        except CanonicalJSONError as exc:
            raise bad_patch(f"request body is not valid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise bad_patch("request body must be a JSON object")

        query_clock = request.args.get("now_ms") or request.args.get(
            "logical_clock_ms"
        )
        if "patch" in body:
            patch_value = body["patch"]
            now_value = body.get("logical_clock_ms", query_clock)
        else:
            patch_value = dict(body)
            now_value = patch_value.pop("logical_clock_ms", query_clock)
        if now_value is None:
            raise bad_clock(
                "logical_clock_ms is required in the request body or query string"
            )
        if isinstance(now_value, str):
            try:
                now_value = int(now_value)
            except ValueError as exc:
                raise bad_clock("logical_clock_ms must be an integer") from exc
        if isinstance(now_value, bool) or not isinstance(now_value, int):
            raise bad_clock("logical_clock_ms must be an integer")
        return _response(hub.apply_patch(patch_value, now_value))

    return app


def validate_loopback_host(host: str) -> None:
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigHubError(
            "E_BAD_HOST",
            "HTTP server host must be a loopback address",
            http_status=400,
        ) from exc
    if not address.is_loopback:
        raise ConfigHubError(
            "E_BAD_HOST",
            "HTTP server may only listen on a loopback address",
            http_status=400,
        )
