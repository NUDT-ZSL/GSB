"""Loopback JSON-lines dispatch client."""

from __future__ import annotations

import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .clock import CLIENT_CLOCK_BASIS, mono_ms, mono_ns
from .config import ClusterSpec, TaskSpec
from .errors import E_SERVICE, E_VERSION, BusinessError
from .protocol import MAX_LINE_BYTES, PROTOCOL_VERSION, decode_message, encode_message


@dataclass(frozen=True)
class DispatchOutcome:
    request: dict[str, Any]
    response: dict[str, Any]
    attempts: int
    started_mono_ms: int
    finished_mono_ms: int
    started_mono_ns: int
    finished_mono_ns: int
    attempt_errors: tuple[str, ...]

    @property
    def client_elapsed_ms(self) -> int:
        return (self.finished_mono_ns - self.started_mono_ns) // 1_000_000


class DispatchClient:
    def __init__(self, *, retry_backoff_ms: int = 10) -> None:
        self.retry_backoff_ms = max(0, retry_backoff_ms)

    def _request_payload(self, task: TaskSpec, cluster: ClusterSpec,
                         config_version: int, timeout_ms: int,
                         retries: int) -> dict[str, Any]:
        return {
            "type": "dispatch",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": f"{task.task_id}:{cluster.cluster_id}:v{config_version}:{uuid.uuid4().hex}",
            "task_id": task.task_id,
            "cluster_id": cluster.cluster_id,
            "config_version": config_version,
            "config_keys": list(task.config_keys),
            "nodes": list(cluster.nodes),
            "aggregation": {"mode": task.aggregation_mode},
            "timeout_ms": timeout_ms,
            "retries": retries,
        }

    def _single_attempt(self, cluster: ClusterSpec, request: dict[str, Any],
                        timeout_ms: int) -> dict[str, Any]:
        timeout_s = timeout_ms / 1000.0
        family = socket.AF_INET6 if cluster.host == "::1" else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout_s)
        try:
            sock.connect((cluster.host, cluster.port))
            sock.sendall(encode_message(request))
            # This client becomes the active TCP closer. The service waits for
            # this FIN before closing its accepted socket, keeping the service
            # listening port out of TIME_WAIT and immediately restartable.
            sock.shutdown(socket.SHUT_WR)
            chunks = bytearray()
            while True:
                chunk = sock.recv(min(65536, MAX_LINE_BYTES + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
                if b"\n" in chunk:
                    break
                if len(chunks) > MAX_LINE_BYTES:
                    raise OSError("response exceeds maximum line length")
            line, _, _ = bytes(chunks).partition(b"\n")
            if not line:
                raise OSError("service closed connection without a response line")
            response = decode_message(line)
        finally:
            sock.close()
        if not response.get("ok", False):
            code = response.get("code", E_SERVICE)
            error = BusinessError(
                code,
                response.get("message", "service rejected dispatch"),
                field_path=response.get("field_path"),
                details=response.get("details", {}),
            )
            raise error
        required = {"summary", "components", "calculation_duration_ms", "config_version"}
        if not required.issubset(response):
            raise BusinessError(
                E_SERVICE,
                "service response is missing required fields",
                details={"missing": sorted(required - set(response))},
            )
        return response

    def dispatch(self, task: TaskSpec, cluster: ClusterSpec, *,
                 config_version: int | None = None, timeout_ms: int | None = None,
                 retries: int | None = None) -> DispatchOutcome:
        version = cluster.config_version if config_version is None else config_version
        effective_timeout = task.timeout_ms if timeout_ms is None else timeout_ms
        effective_retries = task.retries if retries is None else retries
        request = self._request_payload(task, cluster, version, effective_timeout, effective_retries)
        started_ns = mono_ns()
        started_ms = started_ns // 1_000_000
        errors: list[str] = []
        total_attempts = effective_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                response = self._single_attempt(cluster, request, effective_timeout)
                finished_ns = mono_ns()
                return DispatchOutcome(
                    request=request,
                    response=response,
                    attempts=attempt,
                    started_mono_ms=started_ms,
                    finished_mono_ms=finished_ns // 1_000_000,
                    started_mono_ns=started_ns,
                    finished_mono_ns=finished_ns,
                    attempt_errors=tuple(errors),
                )
            except BusinessError as exc:
                errors.append(f"attempt {attempt}: {exc.code}: {exc.message}")
                if exc.code == E_VERSION:
                    raise
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            if attempt < total_attempts and self.retry_backoff_ms:
                time.sleep((self.retry_backoff_ms * attempt) / 1000.0)
        finished_ns = mono_ns()
        raise BusinessError(
            E_SERVICE,
            f"dispatch failed after {total_attempts} attempt(s)",
            details={
                "task_id": task.task_id,
                "cluster_id": cluster.cluster_id,
                "config_version": version,
                "host": cluster.host,
                "port": cluster.port,
                "timeout_ms": effective_timeout,
                "retries": effective_retries,
                "attempt_errors": errors,
                "started_mono_ms": started_ms,
                "finished_mono_ms": finished_ns // 1_000_000,
                "client_clock_basis": CLIENT_CLOCK_BASIS,
            },
        )
