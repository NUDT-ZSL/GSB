from __future__ import annotations

import http.client
import json
import socket
import ssl
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from .clock import RequestPlan
from .errors import BizError
from .scene import PrecisionConfig, ServiceConfig, TenantConfig
from .util import parse_decimal


@dataclass(frozen=True)
class TransportResult:
    success: bool
    http_status: int | None
    latency_ms: Decimal | None
    error_code: str | None
    error_message: str | None
    error_fields: dict[str, Any]
    raw_body: str | None


class LoopbackHTTPTransport:
    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self.timeout_seconds = timeout_seconds

    def execute(
        self,
        service: ServiceConfig,
        tenant: TenantConfig,
        request: RequestPlan,
        precision: PrecisionConfig,
    ) -> TransportResult:
        path = _request_path(service, tenant, request)
        if service.scheme == "https":
            connection = http.client.HTTPSConnection(
                service.host,
                service.port,
                timeout=self.timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                service.host,
                service.port,
                timeout=self.timeout_seconds,
            )
        headers = {
            "Accept": "application/json",
            "Connection": "close",
            "X-Tenant": request.tenant,
            "X-Service": request.service,
            "X-Batch": request.batch_id,
            "X-Gradient-Index": str(request.gradient_index),
            "X-Concurrency": str(request.concurrency),
            "X-Wave-Index": str(request.wave_index),
            "X-Request-Index": str(request.request_index),
            "X-Request-Id": request.request_id,
            "X-Logical-Ms": str(request.release_ms),
            "X-Window-Index": str(request.window_index),
        }
        body: bytes | None = None
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            body = response.read()
            status = response.status
            latency_header = response.getheader("X-Simulated-Latency-Us")
        except ConnectionRefusedError as exc:
            return _failure(
                "E_CONNECTION_REFUSED",
                "connection refused by target",
                request,
                reason=str(exc),
            )
        except http.client.RemoteDisconnected as exc:
            return _failure(
                "E_TARGET_INTERRUPTED",
                "target connection closed before a response was received",
                request,
                reason=str(exc),
            )
        except ConnectionResetError as exc:
            return _failure(
                "E_CONNECTION_REFUSED",
                "target reset the connection",
                request,
                reason=str(exc),
            )
        except (BrokenPipeError, socket.timeout, TimeoutError) as exc:
            return _failure(
                "E_TARGET_INTERRUPTED",
                "target connection was interrupted",
                request,
                reason=str(exc),
            )
        except OSError as exc:
            code = "E_CONNECTION_REFUSED" if getattr(exc, "errno", None) in {111, 10061} else "E_TARGET_INTERRUPTED"
            return _failure(code, "target request failed", request, reason=str(exc), errno=getattr(exc, "errno", None))
        finally:
            connection.close()

        body_text = body.decode("utf-8", errors="replace") if body is not None else ""
        body_latency = _extract_latency_ms(body_text)
        header_latency = _parse_latency_us(latency_header)
        latency_ms = body_latency if body_latency is not None else header_latency
        if status in {502, 503, 504}:
            return TransportResult(
                success=False,
                http_status=status,
                latency_ms=latency_ms,
                error_code="E_TARGET_INTERRUPTED",
                error_message=f"target returned interruption status {status}",
                error_fields={
                    "request_id": request.request_id,
                    "http_status": status,
                    "logical_ms": request.release_ms,
                    "body_prefix": body_text[:256],
                },
                raw_body=body_text,
            )
        if status not in tenant.success_status_codes:
            return TransportResult(
                success=False,
                http_status=status,
                latency_ms=latency_ms,
                error_code="E_STATUS_CODE_MISMATCH",
                error_message=f"unexpected status code {status}",
                error_fields={
                    "request_id": request.request_id,
                    "http_status": status,
                    "expected_status_codes": list(tenant.success_status_codes),
                    "logical_ms": request.release_ms,
                    "body_prefix": body_text[:256],
                },
                raw_body=body_text,
            )
        if not body_text.strip():
            return TransportResult(
                success=False,
                http_status=status,
                latency_ms=None,
                error_code="E_MALFORMED_RESPONSE",
                error_message="empty response body",
                error_fields={
                    "request_id": request.request_id,
                    "http_status": status,
                    "logical_ms": request.release_ms,
                    "expected": "JSON object with simulated latency",
                },
                raw_body=body_text,
            )
        try:
            payload = json.loads(body_text, parse_float=Decimal)
        except Exception as exc:
            return TransportResult(
                success=False,
                http_status=status,
                latency_ms=None,
                error_code="E_MALFORMED_RESPONSE",
                error_message="response body is not valid JSON",
                error_fields={
                    "request_id": request.request_id,
                    "http_status": status,
                    "logical_ms": request.release_ms,
                    "body_prefix": body_text[:256],
                    "reason": str(exc),
                },
                raw_body=body_text,
            )
        if not isinstance(payload, dict):
            return TransportResult(
                success=False,
                http_status=status,
                latency_ms=None,
                error_code="E_MALFORMED_RESPONSE",
                error_message="response JSON is not an object",
                error_fields={
                    "request_id": request.request_id,
                    "http_status": status,
                    "logical_ms": request.release_ms,
                },
                raw_body=body_text,
            )
        latency_ms = _latency_from_payload(payload)
        if latency_ms is None or latency_ms < 0:
            return TransportResult(
                success=False,
                http_status=status,
                latency_ms=None,
                error_code="E_MALFORMED_RESPONSE",
                error_message="response does not contain a valid simulated latency",
                error_fields={
                    "request_id": request.request_id,
                    "http_status": status,
                    "logical_ms": request.release_ms,
                    "required_field": "simulated_latency_us",
                },
                raw_body=body_text,
            )
        if latency_ms > precision.request_logical_budget_ms:
            return TransportResult(
                success=False,
                http_status=status,
                latency_ms=latency_ms,
                error_code="E_REQUEST_BUDGET_EXCEEDED",
                error_message="response latency exceeds the per-request logical budget",
                error_fields={
                    "request_id": request.request_id,
                    "http_status": status,
                    "logical_ms": request.release_ms,
                    "latency_ms": str(latency_ms),
                    "request_logical_budget_ms": str(precision.request_logical_budget_ms),
                },
                raw_body=body_text,
            )
        return TransportResult(
            success=True,
            http_status=status,
            latency_ms=latency_ms,
            error_code=None,
            error_message=None,
            error_fields={},
            raw_body=body_text,
        )


def _request_path(service: ServiceConfig, tenant: TenantConfig, request: RequestPlan) -> str:
    base = service.base_path.rstrip("/")
    tenant_path = f"/tenant/{quote(request.tenant, safe='')}/request"
    return f"{base}{tenant_path}"


def _extract_latency_ms(body_text: str) -> Decimal | None:
    if not body_text.strip():
        return None
    try:
        payload = json.loads(body_text, parse_float=Decimal)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return _latency_from_payload(payload)


def _latency_from_payload(payload: dict[str, Any]) -> Decimal | None:
    if "simulated_latency_us" in payload:
        try:
            value = parse_decimal(payload["simulated_latency_us"], "simulated_latency_us")
        except BizError:
            return None
        return value / Decimal(1000)
    if "latency_ms" in payload:
        try:
            return parse_decimal(payload["latency_ms"], "latency_ms")
        except BizError:
            return None
    return None


def _parse_latency_us(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        us = parse_decimal(value, "X-Simulated-Latency-Us")
    except BizError:
        return None
    if us < 0:
        return None
    return us / Decimal(1000)


def _failure(code: str, message: str, request: RequestPlan, **fields: Any) -> TransportResult:
    return TransportResult(
        success=False,
        http_status=None,
        latency_ms=None,
        error_code=code,
        error_message=message,
        error_fields={
            "request_id": request.request_id,
            "logical_ms": request.release_ms,
            **fields,
        },
        raw_body=None,
    )

