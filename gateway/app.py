from __future__ import annotations

import argparse
import ipaddress
import json
import os
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import BadRequest

from .config import parse_config
from .errors import GatewayError
from .service import GatewayResponse, GatewayService


def _is_loopback_remote(remote_addr: str | None) -> bool:
    if not remote_addr:
        return False
    try:
        return ipaddress.ip_address(remote_addr).is_loopback
    except ValueError:
        return False


def _admin_guard() -> Response | tuple[Response, int] | None:
    if _is_loopback_remote(request.remote_addr):
        return None
    return (
        jsonify(
            {
                "code": "E_ADMIN_LOOPBACK_ONLY",
                "message": "admin endpoints are only available from loopback clients",
            }
        ),
        403,
    )


def _admin_error(error: GatewayError) -> tuple[Response, int]:
    body = {
        "code": error.code,
        "message": error.message,
        "stage": error.stage,
        "details": error.details,
    }
    return jsonify(body), error.http_status


def _window_digest(chain: list[dict[str, Any]]) -> str | None:
    for item in reversed(chain):
        if item.get("stage") == "limit":
            window = item.get("window")
            if isinstance(window, dict):
                return str(window.get("window_digest"))
    return None


def _flask_response(gateway_response: GatewayResponse, request_id: str | None) -> Response:
    headers: list[tuple[str, str]] = []
    has_content_type = False
    for key, value in gateway_response.headers:
        if key.lower() in {
            "content-length",
            "transfer-encoding",
            "connection",
            "keep-alive",
        }:
            continue
        if key.lower().startswith("x-gateway-"):
            continue
        if key.lower() == "content-type":
            has_content_type = True
        headers.append((key, value))
    if not has_content_type:
        headers.append(("Content-Type", "application/octet-stream"))
    headers.append(
        ("X-Gateway-Config-Version", str(gateway_response.config_version or ""))
    )
    headers.append(("X-Gateway-Config-Digest", gateway_response.config_digest or ""))
    headers.append(("X-Gateway-Request-Id", request_id or ""))
    if gateway_response.effective_time_ms is not None:
        headers.append(
            ("X-Gateway-Logical-Time-Ms", str(gateway_response.effective_time_ms))
        )
    headers.append(
        (
            "X-Gateway-Decision",
            "allow"
            if gateway_response.code is None and gateway_response.status < 400
            else "deny",
        )
    )
    if gateway_response.code:
        headers.append(("X-Gateway-Code", gateway_response.code))
    digest = _window_digest(gateway_response.decision_chain)
    if digest:
        headers.append(("X-Gateway-Window-Digest", digest))
    if any(item.get("stage") == "idempotency" for item in gateway_response.decision_chain):
        headers.append(("X-Gateway-Replayed", "true"))
    return Response(
        gateway_response.body,
        status=gateway_response.status,
        headers=headers,
    )


def create_app(
    *,
    database_path: str | os.PathLike[str] | None = None,
    initial_config_path: str | os.PathLike[str] | None = None,
    install_initial_config: bool = True,
    service: GatewayService | None = None,
) -> Flask:
    app = Flask(__name__)
    if service is None:
        database = database_path or os.environ.get("GATEWAY_DB", "var/gateway.sqlite")
        config_path = initial_config_path or os.environ.get("GATEWAY_CONFIG")
        service = GatewayService(
            database,
            initial_config_path=config_path,
            install_initial_config=install_initial_config,
        )
    app.extensions["gateway_service"] = service

    @app.get("/__health")
    def health() -> Response:
        meta = service.store.config_meta()
        status = "ok" if meta is not None else "no_config"
        return jsonify(
            {
                "status": status,
                "active_config": meta,
                "logical_clock_ms": service.store.state_snapshot()["logical_clock_ms"],
            }
        )

    @app.route("/__admin/config", methods=["GET", "POST"])
    def admin_config() -> Any:
        denied = _admin_guard()
        if denied is not None:
            return denied
        if request.method == "GET":
            meta = service.store.config_meta()
            if meta is None:
                return jsonify({"code": "E_NO_CONFIG", "message": "no active config"}), 503
            return jsonify(
                {
                    "active_config": meta,
                    "recovery": service.recovery_report,
                }
            )
        try:
            payload = request.get_json(force=True)
            if not isinstance(payload, dict):
                raise GatewayError(
                    "E_BAD_REQUEST",
                    "admin config body must be a JSON object",
                    400,
                    "config",
                )
            if "config" not in payload:
                raise GatewayError(
                    "E_BAD_REQUEST",
                    "admin config body requires a config field",
                    400,
                    "config",
                )
            if "expected_current_version" not in payload:
                raise GatewayError(
                    "E_BAD_REQUEST",
                    "admin config body requires expected_current_version",
                    400,
                    "config",
                )
            digest = payload.get("content_digest")
            if digest is None:
                digest = request.headers.get("X-Config-Digest")
            if digest is None:
                raise GatewayError(
                    "E_CONFIG_DIGEST_MISMATCH",
                    "content_digest is required for an atomic config submission",
                    409,
                    "config",
                )
            result = service.update_config(
                payload["config"],
                expected_current_version=payload["expected_current_version"],
                provided_digest=digest,
                logical_time_ms=payload.get("logical_time_ms", 0),
            )
            installed = parse_config(payload["config"])
            return jsonify(
                {
                    **result,
                    "content_digest": installed.digest,
                    "route_count": len(installed.routes),
                }
            )
        except GatewayError as error:
            return _admin_error(error)
        except (BadRequest, json.JSONDecodeError, UnicodeDecodeError):
            return _admin_error(
                GatewayError(
                    "E_BAD_REQUEST",
                    "admin config body is not valid JSON",
                    400,
                    "config",
                )
            )

    @app.get("/__admin/state")
    def admin_state() -> Any:
        denied = _admin_guard()
        if denied is not None:
            return denied
        tenant = request.args.get("tenant")
        return jsonify(service.state_snapshot(tenant))

    @app.post("/__admin/recover")
    def admin_recover() -> Any:
        denied = _admin_guard()
        if denied is not None:
            return denied
        report = service.store.recover_pending()
        service.recovery_report = report
        return jsonify(report)

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    def gateway(path: str) -> Response:
        body = request.get_data(cache=True)
        response = service.handle_request(
            method=request.method,
            host=request.host,
            path=request.path,
            query_string=request.query_string.decode("latin-1"),
            headers=dict(request.headers.items()),
            body=body,
        )
        return _flask_response(response, request.headers.get("X-Request-Id"))

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic local gateway")
    parser.add_argument("--db", default=os.environ.get("GATEWAY_DB", "var/gateway.sqlite"))
    parser.add_argument("--config", default=os.environ.get("GATEWAY_CONFIG"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("GATEWAY_PORT", "18080")))
    parser.add_argument("--no-install-config", action="store_true")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("gateway must listen on a loopback address")
    app = create_app(
        database_path=args.db,
        initial_config_path=args.config,
        install_initial_config=not args.no_install_config,
    )
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
