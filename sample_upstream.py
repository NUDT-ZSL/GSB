from __future__ import annotations

import argparse
import json
import time

from flask import Flask, Response, jsonify, request

app = Flask(__name__)


@app.get("/v1/orders")
@app.get("/v1/orders/<path:rest>")
def orders(rest: str = "") -> Response:
    if rest == "slow":
        delay_ms = int(request.args.get("delay_ms", "1000"))
        time.sleep(delay_ms / 1000.0)
    if rest == "malformed":
        return Response("not-a-valid-http-payload", status=200, content_type="text/plain")
    code = int(request.args.get("status", "200"))
    return jsonify(
        {
            "upstream": "orders",
            "path": request.path,
            "tenant": request.headers.get("X-Gateway-Tenant"),
            "request_id": request.headers.get("X-Gateway-Request-Id"),
            "status": code,
        }
    ), code


@app.get("/v1/inventory")
@app.get("/v1/inventory/<path:rest>")
def inventory(rest: str = "") -> Response:
    code = int(request.args.get("status", "200"))
    return jsonify(
        {
            "upstream": "inventory",
            "path": request.path,
            "tenant": request.headers.get("X-Gateway-Tenant"),
            "request_id": request.headers.get("X-Gateway-Request-Id"),
            "status": code,
        }
    ), code


@app.get("/raw/malformed")
def malformed_hint() -> Response:
    return Response(
        json.dumps({"hint": "use tests/malformed_upstream.py for a raw malformed response"}),
        content_type="application/json",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic loopback Flask upstream")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("upstream must listen on a loopback address")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
