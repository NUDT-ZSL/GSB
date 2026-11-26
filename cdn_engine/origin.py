from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from .hashing import sha256_digest


class DeterministicOrigin:
    def __init__(self, content: Dict[str, str]) -> None:
        self.content = content

    def body_for(self, rule_name: str) -> Optional[bytes]:
        if rule_name not in self.content:
            return None
        return str(self.content[rule_name]).encode("utf-8")


def load_origin_content(path: str | Path) -> Dict[str, str]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict) and "content" in payload:
        payload = payload["content"]
    if not isinstance(payload, dict):
        raise ValueError("origin content must be an object mapping rule name to text")
    return {str(key): str(value) for key, value in payload.items()}


def make_handler(origin: DeterministicOrigin, content_path: Optional[Path] = None):
    class OriginHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, status: int, body: bytes, rule: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-CDN-Origin", "deterministic-seed")
            self.send_header("X-CDN-Rule", rule)
            if status == 200:
                self.send_header("X-Content-Digest", sha256_digest(body))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
            parsed = urlparse(self.path)
            prefix = "/verify/"
            if not parsed.path.startswith(prefix):
                self._send(404, b"unknown origin path", parsed.path)
                return
            rule = unquote(parsed.path[len(prefix) :])
            content = origin.content
            if content_path is not None and content_path.exists():
                try:
                    content = load_origin_content(content_path)
                    origin.content = content
                except Exception:
                    pass
            body = origin.body_for(rule)
            if body is None:
                self._send(404, b"unknown rule", rule)
                return
            self._send(200, body, rule)

    return OriginHandler


def start_origin_server(
    content: Dict[str, str],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    content_path: Optional[Path] = None,
) -> ThreadingHTTPServer:
    origin = DeterministicOrigin(content)
    server = ThreadingHTTPServer((host, port), make_handler(origin, content_path))
    thread = threading.Thread(target=server.serve_forever, name="cdn-origin", daemon=True)
    thread.start()
    return server


def arbitrate_origin_digest(
    *,
    base_url: str,
    rule_name: str,
    observed_digest: Optional[str],
    node_name: str,
    clock_ms: int,
    timeout_ms: int = 1000,
) -> Dict[str, Any]:
    url = base_url.rstrip("/") + "/verify/" + quote(rule_name, safe="")
    request_headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "cdn-engine-verifier/1.0",
        "X-CDN-Node": node_name,
        "X-CDN-Rule": rule_name,
        "X-Logical-Clock": str(clock_ms),
    }
    request = Request(url, headers=request_headers, method="GET")
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=max(timeout_ms / 1000.0, 0.05)) as response:
            body = response.read()
            response_digest = sha256_digest(body)
            response_headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower()
                in {
                    "content-type",
                    "content-length",
                    "x-content-digest",
                    "x-cdn-origin",
                    "x-cdn-rule",
                }
            }
            match = observed_digest is not None and response_digest == observed_digest
            return {
                "arbitration": "origin_digest",
                "url": url,
                "request_headers": request_headers,
                "response_status": response.status,
                "response_headers": response_headers,
                "response_digest": response_digest,
                "observed_digest": observed_digest,
                "digest_match": match,
                "cache_hit": match,
                "error_code": None,
            }
    except HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return {
            "arbitration": "origin_digest",
            "url": url,
            "request_headers": request_headers,
            "response_status": exc.code,
            "response_digest": sha256_digest(body) if body else None,
            "observed_digest": observed_digest,
            "digest_match": False,
            "cache_hit": False,
            "error_code": "ORIGIN_HTTP_ERROR",
            "error": str(exc),
        }
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        refused = isinstance(reason, ConnectionRefusedError) or "Connection refused" in str(reason)
        return {
            "arbitration": "origin_digest",
            "url": url,
            "request_headers": request_headers,
            "response_status": None,
            "response_digest": None,
            "observed_digest": observed_digest,
            "digest_match": False,
            "cache_hit": False,
            "error_code": "ORIGIN_CONNECTION_REFUSED" if refused else "ORIGIN_REQUEST_FAILED",
            "error": str(reason),
        }
    except (TimeoutError, OSError) as exc:
        refused = isinstance(exc, ConnectionRefusedError)
        return {
            "arbitration": "origin_digest",
            "url": url,
            "request_headers": request_headers,
            "response_status": None,
            "response_digest": None,
            "observed_digest": observed_digest,
            "digest_match": False,
            "cache_hit": False,
            "error_code": "ORIGIN_CONNECTION_REFUSED" if refused else "ORIGIN_REQUEST_FAILED",
            "error": str(exc),
        }
