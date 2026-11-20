"""Local loopback HTTP test service for upstream error-injection tests.

Deterministic: it never sleeps and never uses randomness; the declared
response latency is a logical value carried in the payload. Faults are
driven by the request body so tests are reproducible.

Usage: python -m gateway.upstream_server --port 8377
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from .upstream import expected_digest


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "E_SCHEMA_SERVICE"}')
            return
        fault = body.get("fault")
        service_ms = int(body.get("service_ms", 0))
        if fault == "budget_exceeded":
            service_ms = service_ms + 10 ** 9
        digest = expected_digest(body.get("service", ""),
                                 body.get("request_id", ""),
                                 body.get("tokens", 0))
        if fault == "digest_mismatch":
            digest = "f" * 64
        payload = json.dumps({"digest": digest,
                              "response_ms": service_ms}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep test output deterministic
        pass


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8377)
    args = parser.parse_args(argv)
    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"loopback upstream listening on 127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
