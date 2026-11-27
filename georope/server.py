"""Flask application for the local geospatial RoPE service."""

from __future__ import annotations

from typing import Any

from flask import Flask, jsonify, request
from werkzeug.exceptions import BadRequest, HTTPException

from .errors import BusinessError, invalid_input
from .generator import generate_sample
from .jsonutil import to_jsonable
from .service import check_request, encode_request, incremental_request


def _json_body() -> dict[str, Any]:
    if not request.is_json:
        raise BusinessError(
            "CONTENT_TYPE_INVALID",
            "request must use application/json",
            [{"expected": "application/json", "actual": request.content_type}],
            415,
        )
    try:
        payload = request.get_json()
    except BadRequest:
        raise BusinessError(
            "JSON_INVALID",
            "request body is not valid JSON",
            [],
            400,
        ) from None
    if not isinstance(payload, dict):
        raise invalid_input("request body must be a JSON object")
    return payload


def create_app() -> Flask:
    app = Flask(__name__)
    app.json.sort_keys = True
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

    @app.errorhandler(BusinessError)
    def handle_business_error(error: BusinessError) -> tuple[Any, int]:
        return jsonify(to_jsonable(error.to_dict())), error.http_status

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException) -> tuple[Any, int]:
        code = f"HTTP_{error.code}"
        return (
            jsonify(
                to_jsonable(
                    {
                        "ok": False,
                        "error": {
                            "code": code,
                            "message": error.description,
                            "locations": [],
                        },
                    }
                )
            ),
            error.code or 500,
        )

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception) -> tuple[Any, int]:
        app.logger.exception("unexpected encoding service failure")
        return (
            jsonify(
                to_jsonable(
                    {
                        "ok": False,
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": str(error),
                            "locations": [],
                        },
                    }
                )
            ),
            500,
        )

    @app.get("/")
    def index() -> Any:
        return jsonify(
            {
                "ok": True,
                "service": "georope",
                "endpoints": {
                    "health": "GET /api/health",
                    "encode": "POST /api/encode",
                    "check": "POST /api/check",
                    "incremental": "POST /api/incremental",
                    "sample": "GET /api/sample?seed=42",
                },
            }
        )

    @app.get("/api/health")
    def health() -> Any:
        return jsonify(
            {
                "ok": True,
                "status": "ready",
                "schema_versions": ["v1", "v2", "v3"],
                "capabilities": [
                    "schema_normalization",
                    "global_rope_encoding",
                    "field_stitching",
                    "seam_continuity",
                    "band_combinations",
                    "incremental_recomputation",
                    "cross_version_verification",
                    "reference_verification",
                ],
            }
        )

    @app.get("/api/sample")
    def sample() -> Any:
        seed_text = request.args.get("seed", "42")
        try:
            seed = int(seed_text)
        except ValueError:
            raise invalid_input("seed must be an integer", {"query": "seed"}) from None
        return jsonify(to_jsonable(generate_sample(seed)))

    @app.post("/api/encode")
    def encode() -> Any:
        return jsonify(to_jsonable(encode_request(_json_body())))

    @app.post("/api/check")
    def check() -> Any:
        return jsonify(to_jsonable(check_request(_json_body())))

    @app.post("/api/incremental")
    def incremental() -> Any:
        return jsonify(to_jsonable(incremental_request(_json_body())))

    app.add_url_rule("/encode", "encode_alias", encode, methods=["POST"])
    app.add_url_rule("/check", "check_alias", check, methods=["POST"])
    app.add_url_rule(
        "/incremental", "incremental_alias", incremental, methods=["POST"]
    )
    return app
