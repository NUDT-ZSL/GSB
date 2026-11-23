from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib import error, parse, request

from .errors import BusinessError


@dataclass
class OperationResult:
    operation_id: str
    ok: bool
    code: str | None = None
    message: str = ""
    duration_ms: int | None = None
    source: str = "post"
    response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "duration_ms": self.duration_ms,
            "source": self.source,
            "response": self.response,
        }


class LaunchClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._opener = request.build_opener(request.ProxyHandler({}))

    def launch(
        self,
        *,
        operation_id: str,
        service: str,
        replica_id: str,
        model_version: str,
        cold_start_budget_ms: int,
        round_no: int,
    ) -> OperationResult:
        payload = {
            "operation_id": operation_id,
            "service": service,
            "replica_id": replica_id,
            "model_version": model_version,
            "cold_start_budget_ms": cold_start_budget_ms,
            "round_no": round_no,
        }
        timeout = max(40.0, (cold_start_budget_ms * 4) / 1000.0)
        started = time.monotonic()
        try:
            response = self._request_json(
                "POST", "/launch", payload=payload, timeout=timeout
            )
        except error.HTTPError as exc:
            return self._http_error_result(operation_id, exc, source="post")
        except error.URLError as exc:
            return self._url_error_result(operation_id, exc, source="post")
        except TimeoutError as exc:
            return OperationResult(
                operation_id=operation_id,
                ok=False,
                code="E_LAUNCH_HTTP_ERROR",
                message=f"launch request timed out: {exc}",
                source="post",
            )
        return self._classify(
            operation_id,
            response,
            cold_start_budget_ms=cold_start_budget_ms,
            source="post",
            wall_elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    def query_operation(
        self, operation_id: str, *, cold_start_budget_ms: int | None = None
    ) -> OperationResult | None:
        path = "/operations/" + parse.quote(operation_id, safe="")
        try:
            response = self._request_json("GET", path, timeout=5.0)
        except error.HTTPError as exc:
            if exc.code == 404:
                return None
            return self._http_error_result(operation_id, exc, source="get")
        except error.URLError as exc:
            return self._url_error_result(operation_id, exc, source="get")
        return self._classify(
            operation_id,
            response,
            cold_start_budget_ms=cold_start_budget_ms,
            source="get",
            wall_elapsed_ms=None,
        )

    def recover_operation(
        self,
        *,
        operation_id: str,
        service: str,
        replica_id: str,
        model_version: str,
        cold_start_budget_ms: int,
        round_no: int,
    ) -> OperationResult:
        existing = self.query_operation(
            operation_id, cold_start_budget_ms=cold_start_budget_ms
        )
        if existing is not None:
            existing.source = "recovered_get"
            return existing
        result = self.launch(
            operation_id=operation_id,
            service=service,
            replica_id=replica_id,
            model_version=model_version,
            cold_start_budget_ms=cold_start_budget_ms,
            round_no=round_no,
        )
        result.source = "recovered_post"
        return result

    def health(self, operation_id: str) -> OperationResult:
        path = "/health?" + parse.urlencode({"operation_id": operation_id})
        try:
            response = self._request_json("GET", path, timeout=5.0)
        except error.HTTPError as exc:
            return self._http_error_result(operation_id, exc, source="health")
        except error.URLError as exc:
            return self._url_error_result(operation_id, exc, source="health")
        return self._classify(
            operation_id,
            response,
            cold_start_budget_ms=None,
            source="health",
            wall_elapsed_ms=None,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        with self._opener.open(req, timeout=timeout) as response:
            raw = response.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                "E_LAUNCH_RESPONSE_INVALID",
                f"launch target returned non-JSON data: {exc}",
            ) from exc
        if not isinstance(parsed, dict):
            raise BusinessError(
                "E_LAUNCH_RESPONSE_INVALID",
                "launch target response must be a JSON object",
            )
        return parsed

    def _classify(
        self,
        operation_id: str,
        response: dict[str, Any],
        *,
        cold_start_budget_ms: int | None,
        source: str,
        wall_elapsed_ms: int | None,
    ) -> OperationResult:
        response_operation = response.get("operation_id")
        if response_operation not in {None, operation_id}:
            return OperationResult(
                operation_id=operation_id,
                ok=False,
                code="E_LAUNCH_RESPONSE_INVALID",
                message="launch target returned a different operation id",
                source=source,
                response=response,
            )
        duration_ms = response.get("duration_ms")
        if duration_ms is not None and (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, (int, float))
            or duration_ms < 0
        ):
            return OperationResult(
                operation_id=operation_id,
                ok=False,
                code="E_LAUNCH_RESPONSE_INVALID",
                message="launch target returned an invalid duration_ms",
                source=source,
                response=response,
            )
        logical_duration = int(duration_ms) if duration_ms is not None else None
        if (
            cold_start_budget_ms is not None
            and logical_duration is not None
            and logical_duration > cold_start_budget_ms
        ):
            return OperationResult(
                operation_id=operation_id,
                ok=False,
                code="E_COLD_START_BUDGET_EXCEEDED",
                message="cold start exceeded its per-replica logical budget",
                duration_ms=logical_duration,
                source=source,
                response=response,
            )
        if not response.get("ok", False):
            return OperationResult(
                operation_id=operation_id,
                ok=False,
                code=str(response.get("error_code") or "E_LAUNCH_HTTP_ERROR"),
                message=str(response.get("message") or "launch target reported failure"),
                duration_ms=logical_duration,
                source=source,
                response=response,
            )
        return OperationResult(
            operation_id=operation_id,
            ok=True,
            duration_ms=logical_duration,
            source=source,
            response=response,
        )

    def _url_error_result(
        self, operation_id: str, exc: error.URLError, *, source: str
    ) -> OperationResult:
        reason = exc.reason
        if isinstance(reason, ConnectionRefusedError) or (
            isinstance(reason, OSError)
            and getattr(reason, "errno", None) in {61, 111, 10061}
        ):
            return OperationResult(
                operation_id=operation_id,
                ok=False,
                code="E_LAUNCH_CONNECTION_REFUSED",
                message="launch target refused the connection",
                source=source,
            )
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return OperationResult(
                operation_id=operation_id,
                ok=False,
                code="E_LAUNCH_HTTP_ERROR",
                message="launch target request timed out",
                source=source,
            )
        return OperationResult(
            operation_id=operation_id,
            ok=False,
            code="E_STUB_UNAVAILABLE",
            message=f"launch target is unavailable: {reason}",
            source=source,
        )

    def _http_error_result(
        self, operation_id: str, exc: error.HTTPError, *, source: str
    ) -> OperationResult:
        return OperationResult(
            operation_id=operation_id,
            ok=False,
            code="E_LAUNCH_HTTP_ERROR",
            message=f"launch target returned HTTP {exc.code}",
            source=source,
            response={"http_status": exc.code},
        )
