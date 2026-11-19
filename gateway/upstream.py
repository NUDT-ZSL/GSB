from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from .errors import GatewayError

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_REQUEST_DROP = _HOP_BY_HOP | {
    "host",
    "content-length",
    "authorization",
    "x-api-key",
}


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


def forward_request(
    *,
    upstream: str,
    method: str,
    path: str,
    query_string: str,
    headers: Mapping[str, str],
    body: bytes,
    budget_ms: int,
    request_id: str,
    tenant: str,
    config_version: int,
    logical_time_ms: int,
) -> UpstreamResponse:
    parsed = urlsplit(upstream)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        raise GatewayError(
            "E_UPSTREAM_MALFORMED",
            "validated upstream URL is unexpectedly invalid",
            502,
            "upstream",
            {"upstream": upstream},
        )
    target = path or "/"
    if query_string:
        target = f"{target}?{query_string}"
    forwarded: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in _REQUEST_DROP or lower.startswith("proxy-") or lower.startswith("x-gateway-"):
            continue
        forwarded[key] = value
    forwarded["X-Gateway-Request-Id"] = request_id
    forwarded["X-Gateway-Tenant"] = tenant
    forwarded["X-Gateway-Config-Version"] = str(config_version)
    forwarded["X-Gateway-Logical-Time-Ms"] = str(logical_time_ms)
    timeout = max(budget_ms / 1000.0, 0.001)
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(method, target, body=body, headers=forwarded)
        response = connection.getresponse()
        declared_length: int | None = None
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError as exc:
                raise GatewayError(
                    "E_UPSTREAM_MALFORMED",
                    "upstream returned an invalid Content-Length",
                    502,
                    "upstream",
                    {"reason": "invalid_content_length", "upstream": upstream},
                ) from exc
            if declared < 0 or declared > MAX_RESPONSE_BYTES:
                raise GatewayError(
                    "E_UPSTREAM_MALFORMED",
                    "upstream response size is outside the accepted range",
                    502,
                    "upstream",
                    {
                        "reason": "response_too_large",
                        "content_length": declared,
                        "max_response_bytes": MAX_RESPONSE_BYTES,
                    },
                )
            declared_length = declared
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise GatewayError(
                "E_UPSTREAM_MALFORMED",
                "upstream response exceeded the maximum accepted size",
                502,
                "upstream",
                {"reason": "response_too_large", "max_response_bytes": MAX_RESPONSE_BYTES},
            )
        bodyless_response = method.upper() == "HEAD" or response.status in {204, 304}
        if (
            declared_length is not None
            and not bodyless_response
            and len(payload) != declared_length
        ):
            raise GatewayError(
                "E_UPSTREAM_MALFORMED",
                "upstream response length does not match Content-Length",
                502,
                "upstream",
                {
                    "reason": "content_length_mismatch",
                    "declared_length": declared_length,
                    "received_bytes": len(payload),
                    "upstream": upstream,
                },
            )
        response_headers: list[tuple[str, str]] = []
        for key, value in response.getheaders():
            if key.lower() in _HOP_BY_HOP:
                continue
            response_headers.append((key, value))
        return UpstreamResponse(
            status=int(response.status),
            headers=tuple(response_headers),
            body=payload,
        )
    except GatewayError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise GatewayError(
            "E_UPSTREAM_TIMEOUT",
            "upstream response exceeded the request budget",
            504,
            "upstream",
            {"budget_ms": budget_ms, "upstream": upstream},
        ) from exc
    except ConnectionRefusedError as exc:
        raise GatewayError(
            "E_UPSTREAM_REFUSED",
            "upstream connection was refused",
            502,
            "upstream",
            {"upstream": upstream},
        ) from exc
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
        raise GatewayError(
            "E_UPSTREAM_MALFORMED",
            "upstream reset the connection after it was established",
            502,
            "upstream",
            {"reason": "connection_reset", "upstream": upstream},
        ) from exc
    except http.client.IncompleteRead as exc:
        raise GatewayError(
            "E_UPSTREAM_MALFORMED",
            "upstream closed the connection before the declared response completed",
            502,
            "upstream",
            {
                "reason": "incomplete_read",
                "partial_bytes": len(exc.partial),
                "upstream": upstream,
            },
        ) from exc
    except (http.client.BadStatusLine, http.client.HTTPException, ValueError) as exc:
        raise GatewayError(
            "E_UPSTREAM_MALFORMED",
            "upstream returned a malformed HTTP response",
            502,
            "upstream",
            {"reason": type(exc).__name__, "upstream": upstream},
        ) from exc
    except OSError as exc:
        raise GatewayError(
            "E_UPSTREAM_REFUSED",
            "upstream connection failed",
            502,
            "upstream",
            {"upstream": upstream, "reason": type(exc).__name__, "errno": exc.errno},
        ) from exc
    finally:
        connection.close()
