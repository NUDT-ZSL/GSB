from __future__ import annotations

import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

from .errors import BusinessError, ErrorCode
from .models import CacheEntry, OriginResponse, Policy, SourceResource


_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_STATUS_RE = re.compile(r"^HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?$")
_CACHE_DIRECTIVE_RE = re.compile(r"^(max-age|s-maxage)\s*=\s*\"?([0-9]+)\"?$", re.IGNORECASE)


class OriginRequestError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool,
        details: dict[str, Any] | None = None,
        attempts: int = 1,
        retry_exhausted: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.attempts = attempts
        self.retry_exhausted = retry_exhausted


@dataclass(frozen=True)
class ParsedCacheControl:
    max_age: int | None
    s_maxage: int | None
    effective_ttl_seconds: int


def parse_cache_control_header(value: str | None) -> ParsedCacheControl:
    if value is None or not value.strip():
        return ParsedCacheControl(None, None, 0)
    max_age: int | None = None
    s_maxage: int | None = None
    for part in value.split(","):
        directive = part.strip()
        if not directive:
            raise OriginRequestError(
                ErrorCode.ORIGIN_MALFORMED_HEADERS,
                "empty Cache-Control directive",
                retryable=False,
                details={"cache_control": value},
            )
        name, separator, raw_value = directive.partition("=")
        name = name.strip().lower()
        if name in {"max-age", "s-maxage"}:
            if not separator:
                raise OriginRequestError(
                    ErrorCode.ORIGIN_MALFORMED_HEADERS,
                    f"Cache-Control {name} is missing a value",
                    retryable=False,
                    details={"cache_control": value},
                )
            match = _CACHE_DIRECTIVE_RE.fullmatch(directive)
            if not match:
                raise OriginRequestError(
                    ErrorCode.ORIGIN_MALFORMED_HEADERS,
                    f"invalid Cache-Control {name} value",
                    retryable=False,
                    details={"cache_control": value},
                )
            number = int(match.group(2))
            if match.group(1).lower() == "max-age":
                max_age = number
            else:
                s_maxage = number
        elif separator:
            continue
    values = [number for number in (max_age, s_maxage) if number is not None]
    return ParsedCacheControl(
        max_age=max_age,
        s_maxage=s_maxage,
        effective_ttl_seconds=min(values) if values else 0,
    )


def logical_http_date(clock: int, policy: Policy) -> str:
    raw_epoch = policy.origin_epoch_utc
    try:
        if raw_epoch.endswith("Z"):
            epoch = datetime.fromisoformat(raw_epoch[:-1] + "+00:00")
        else:
            epoch = datetime.fromisoformat(raw_epoch)
        if epoch.tzinfo is None:
            epoch = epoch.replace(tzinfo=timezone.utc)
        epoch = epoch.astimezone(timezone.utc)
        instant = epoch + timedelta(seconds=clock * policy.origin_seconds_per_tick)
    except ValueError:
        instant = datetime.fromtimestamp(0, timezone.utc)
    return format_datetime(instant, usegmt=True)


def _normalize_etag_list(value: str) -> set[str]:
    tags: set[str] = set()
    for part in value.split(","):
        token = part.strip()
        if token.startswith("W/"):
            token = token[2:].strip()
        tags.add(token)
    return tags


def _error(
    code: ErrorCode,
    message: str,
    *,
    retryable: bool,
    details: dict[str, Any] | None = None,
) -> OriginRequestError:
    return OriginRequestError(code, message, retryable=retryable, details=details)


def request_origin(
    *,
    base_url: str,
    entry: CacheEntry,
    policy: Policy,
    timeout_ms: int | None = None,
    extra_headers: dict[str, str] | None = None,
) -> OriginResponse:
    split = urlsplit(base_url)
    if split.scheme != "http" or not split.hostname:
        raise _error(
            ErrorCode.INVALID_ARGUMENT,
            "origin URL must be an http:// loopback URL",
            retryable=False,
            details={"origin": base_url},
        )
    hostname = split.hostname
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise _error(
            ErrorCode.INVALID_ARGUMENT,
            "only loopback origin targets are permitted",
            retryable=False,
            details={"origin": base_url, "hostname": hostname},
        )
    port = split.port or 80
    request_path = entry.path
    if split.path and split.path != "/":
        request_path = split.path.rstrip("/") + entry.path
    budget_ms = timeout_ms if timeout_ms is not None else policy.origin_request_budget_ms
    timeout_seconds = max(0.001, budget_ms / 1000.0)
    if_modified_since = logical_http_date(entry.last_verified_clock, policy)
    headers = {
        "Host": f"{hostname}:{port}",
        "User-Agent": "cdn-edge-workbench/0.1",
        "Accept": "*/*",
        "Connection": "close",
        "If-None-Match": entry.etag,
        "If-Modified-Since": if_modified_since,
        "X-CDN-Edge-Node": entry.node,
        "X-Cached-Version": str(entry.content_version),
    }
    if extra_headers:
        headers.update(extra_headers)
    request_lines = [f"GET {request_path} HTTP/1.1"]
    request_lines.extend(f"{name}: {value}" for name, value in headers.items())
    request_bytes = ("\r\n".join(request_lines) + "\r\n\r\n").encode("utf-8")
    began = time.monotonic()
    try:
        with socket.create_connection((hostname, port), timeout=timeout_seconds) as sock:
            sock.settimeout(timeout_seconds)
            sock.sendall(request_bytes)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > 4 * 1024 * 1024:
                    raise _error(
                        ErrorCode.ORIGIN_BAD_RESPONSE,
                        "origin response exceeds size limit",
                        retryable=False,
                    )
    except socket.timeout as exc:
        elapsed = (time.monotonic() - began) * 1000.0
        raise _error(
            ErrorCode.ORIGIN_REQUEST_TIMEOUT,
            "origin request exceeded the single-request budget",
            retryable=True,
            details={"budget_ms": budget_ms, "elapsed_ms": elapsed},
        ) from exc
    except ConnectionRefusedError as exc:
        raise _error(
            ErrorCode.ORIGIN_CONNECTION_REFUSED,
            "origin connection was refused",
            retryable=True,
            details={"host": hostname, "port": port},
        ) from exc
    except OSError as exc:
        raise _error(
            ErrorCode.ORIGIN_CONNECTION_REFUSED,
            f"origin connection failed: {exc}",
            retryable=True,
            details={"host": hostname, "port": port, "errno": getattr(exc, "errno", None)},
        ) from exc
    elapsed_ms = (time.monotonic() - began) * 1000.0
    if elapsed_ms > budget_ms:
        raise _error(
            ErrorCode.ORIGIN_REQUEST_TIMEOUT,
            "origin response arrived after the single-request budget",
            retryable=True,
            details={"budget_ms": budget_ms, "elapsed_ms": elapsed_ms},
        )
    raw = b"".join(chunks)
    if not raw:
        raise _error(
            ErrorCode.ORIGIN_CONNECTION_REFUSED,
            "origin closed the connection without sending a response",
            retryable=True,
            details={"host": hostname, "port": port},
        )
    header_end = raw.find(b"\r\n\r\n")
    if header_end < 0:
        raise _error(
            ErrorCode.ORIGIN_MALFORMED_HEADERS,
            "origin response has no complete header terminator",
            retryable=False,
            details={"received_bytes": len(raw)},
        )
    header_bytes = raw[:header_end]
    body = raw[header_end + 4 :]
    try:
        header_text = header_bytes.decode("iso-8859-1")
    except UnicodeDecodeError as exc:
        raise _error(
            ErrorCode.ORIGIN_MALFORMED_HEADERS,
            "origin headers are not ISO-8859-1 decodable",
            retryable=False,
        ) from exc
    lines = header_text.split("\r\n")
    if not lines:
        raise _error(
            ErrorCode.ORIGIN_MALFORMED_HEADERS,
            "origin response is missing a status line",
            retryable=False,
        )
    status_match = _STATUS_RE.fullmatch(lines[0])
    if not status_match:
        raise _error(
            ErrorCode.ORIGIN_MALFORMED_HEADERS,
            "origin status line is malformed",
            retryable=False,
            details={"status_line": lines[0]},
        )
    status = int(status_match.group(1))
    parsed_headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or line[0] in " \t":
            raise _error(
                ErrorCode.ORIGIN_MALFORMED_HEADERS,
                "origin header continuation or empty line is malformed",
                retryable=False,
                details={"header_line": line},
            )
        if ":" not in line:
            raise _error(
                ErrorCode.ORIGIN_MALFORMED_HEADERS,
                "origin header line is missing a colon",
                retryable=False,
                details={"header_line": line},
            )
        name, value = line.split(":", 1)
        if not _HEADER_NAME_RE.fullmatch(name):
            raise _error(
                ErrorCode.ORIGIN_MALFORMED_HEADERS,
                "origin header name is invalid",
                retryable=False,
                details={"header_name": name},
            )
        if any(ord(character) < 32 and character != "\t" for character in value):
            raise _error(
                ErrorCode.ORIGIN_MALFORMED_HEADERS,
                "origin header value contains a control character",
                retryable=False,
                details={"header_name": name},
            )
        lower_name = name.lower()
        stripped = value.strip()
        if lower_name in parsed_headers and parsed_headers[lower_name] != stripped:
            raise _error(
                ErrorCode.ORIGIN_MALFORMED_HEADERS,
                "origin response contains conflicting duplicate headers",
                retryable=False,
                details={"header_name": name},
            )
        parsed_headers[lower_name] = stripped
    if "transfer-encoding" in parsed_headers:
        raise _error(
            ErrorCode.ORIGIN_MALFORMED_HEADERS,
            "transfer-encoding responses are not supported",
            retryable=False,
        )
    content_length = parsed_headers.get("content-length")
    if content_length is not None:
        if not content_length.isdigit():
            raise _error(
                ErrorCode.ORIGIN_MALFORMED_HEADERS,
                "Content-Length is not a non-negative integer",
                retryable=False,
            )
        expected_length = int(content_length)
        if expected_length != len(body):
            raise _error(
                ErrorCode.ORIGIN_MALFORMED_HEADERS,
                "Content-Length does not match the received body",
                retryable=False,
                details={"content_length": expected_length, "body_length": len(body)},
            )
    if status not in {200, 304}:
        raise _error(
            ErrorCode.ORIGIN_BAD_RESPONSE,
            f"origin returned unsupported status {status}",
            retryable=False,
            details={"status": status},
        )
    if status == 304 and body:
        raise _error(
            ErrorCode.ORIGIN_MALFORMED_HEADERS,
            "304 response must not include a body",
            retryable=False,
            details={"body_length": len(body)},
        )
    parse_cache_control_header(parsed_headers.get("cache-control"))
    return OriginResponse(
        status=status,
        headers=parsed_headers,
        body=body,
        elapsed_ms=elapsed_ms,
        attempts=1,
    )


def request_origin_with_retries(
    *,
    base_url: str,
    entry: CacheEntry,
    policy: Policy,
    extra_headers: dict[str, str] | None = None,
) -> OriginResponse:
    max_attempts = policy.origin_retry_budget + 1
    last_error: OriginRequestError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = request_origin(
                base_url=base_url,
                entry=entry,
                policy=policy,
                extra_headers=extra_headers,
            )
            return OriginResponse(
                status=response.status,
                headers=response.headers,
                body=response.body,
                elapsed_ms=response.elapsed_ms,
                attempts=attempt,
                retry_exhausted=False,
            )
        except OriginRequestError as exc:
            last_error = exc
            exc.attempts = attempt
            if not exc.retryable or attempt >= max_attempts:
                exc.retry_exhausted = attempt >= max_attempts and exc.retryable
                raise
    assert last_error is not None
    raise last_error


class LocalOriginServer:
    """Programmable loopback origin with deterministic per-path behaviors."""

    def __init__(
        self,
        resources: dict[str, SourceResource],
        *,
        policy: Policy,
        host: str = "127.0.0.1",
    ) -> None:
        self.resources = dict(resources)
        self.policy = policy
        self.host = host
        self.behaviors: dict[tuple[str | None, str], tuple[str, int, dict[str, Any]]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def set_behavior(
        self,
        path: str,
        behavior: str,
        *,
        node: str | None = None,
        delay_ms: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.behaviors[(node, path)] = (behavior, delay_ms, details or {})

    def _resolve_behavior(
        self,
        path: str,
        node: str | None,
    ) -> tuple[str, int, dict[str, Any]]:
        return (
            self.behaviors.get((node, path))
            or self.behaviors.get((None, path))
            or ("normal", 0, {})
        )

    def __enter__(self) -> "LocalOriginServer":
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                node = self.headers.get("X-CDN-Edge-Node")
                behavior, delay_ms, details = server._resolve_behavior(path, node)
                if behavior == "connection_refused":
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                if behavior in {"request_timeout", "response_timeout"}:
                    delay = delay_ms or (server.policy.origin_request_budget_ms + 250)
                    time.sleep(delay / 1000.0)
                    self.close_connection = True
                    return
                elif delay_ms:
                    time.sleep(delay_ms / 1000.0)
                resource = server.resources.get(path)
                if resource is None:
                    body = b"not found"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except BrokenPipeError:
                        pass
                    return
                current_etag = resource.etag
                request_etag = self.headers.get("If-None-Match", "")
                request_version = self.headers.get("X-Cached-Version")
                directives = ["public"]
                if resource.cache_control.max_age is not None:
                    directives.append(f"max-age={resource.cache_control.max_age}")
                if resource.cache_control.s_maxage is not None:
                    directives.append(f"s-maxage={resource.cache_control.s_maxage}")
                cache_control = ", ".join(directives)
                last_modified = logical_http_date(resource.last_modified_clock, server.policy)
                if behavior == "malformed_headers":
                    raw = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/plain\r\n"
                        b"X-Broken-Header\r\n"
                        b"Content-Length: 2\r\n"
                        b"Connection: close\r\n\r\n{}"
                    )
                    try:
                        self.wfile.write(raw)
                    except BrokenPipeError:
                        pass
                    self.close_connection = True
                    return
                if behavior in {"stale_304", "304_entity_changed"}:
                    etag = request_etag or current_etag
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    if request_version is not None:
                        self.send_header("X-Content-Version", request_version)
                    self.send_header("Cache-Control", cache_control)
                    self.send_header("Last-Modified", last_modified)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                if behavior == "unchanged_200":
                    body = resource.body
                    etag = request_etag or current_etag
                    version = request_version or str(resource.content_version)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("ETag", etag)
                    self.send_header("X-Content-Version", version)
                    self.send_header("Cache-Control", cache_control)
                    self.send_header("Last-Modified", last_modified)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except BrokenPipeError:
                        pass
                    return
                if behavior == "content_tamper_same_version":
                    body = resource.body + b"\n<!-- injected tamper -->"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("ETag", request_etag or current_etag)
                    self.send_header("X-Content-Version", request_version or str(resource.content_version))
                    self.send_header("Cache-Control", cache_control)
                    self.send_header("Last-Modified", last_modified)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except BrokenPipeError:
                        pass
                    return
                if request_etag and current_etag in _normalize_etag_list(request_etag):
                    self.send_response(304)
                    self.send_header("ETag", current_etag)
                    self.send_header("X-Content-Version", str(resource.content_version))
                    self.send_header("Cache-Control", cache_control)
                    self.send_header("Last-Modified", last_modified)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                body = resource.body
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", current_etag)
                self.send_header("X-Content-Version", str(resource.content_version))
                self.send_header("Cache-Control", cache_control)
                self.send_header("Last-Modified", last_modified)
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

        self._server = ThreadingHTTPServer((self.host, 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("origin server is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None


def behavior_for_injection(
    injection_type: str,
) -> str:
    aliases = {
        "304_entity_changed": "stale_304",
        "response_timeout": "request_timeout",
        "content_tamper": "content_tamper_same_version",
    }
    return aliases.get(injection_type, injection_type)

