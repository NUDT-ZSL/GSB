from __future__ import annotations

import errno
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import BusinessError, ErrorCode
from .models import canonical_json


@dataclass(frozen=True)
class LifecycleRequest:
    action: str
    replica_id: str
    service_name: str
    model_version: str
    sample_digest: str
    tick: int
    round_id: str
    idempotency_key: str
    node_name: str
    gpu_index: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "replica_id": self.replica_id,
            "service_name": self.service_name,
            "model_version": self.model_version,
            "sample_digest": self.sample_digest,
            "tick": self.tick,
            "round_id": self.round_id,
            "idempotency_key": self.idempotency_key,
            "node_name": self.node_name,
            "gpu_index": self.gpu_index,
            "metadata": self.metadata,
        }


class Launcher(Protocol):
    def execute(
        self,
        request: LifecycleRequest,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class HTTPControlClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    def execute(
        self,
        request: LifecycleRequest,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if request.action not in {"launch", "drain"}:
            raise BusinessError(
                ErrorCode.LAUNCH_INTERNAL,
                "unsupported replica lifecycle action",
                details={"action": request.action},
            )
        endpoint = f"{self.base_url}/v1/replicas/{request.action}"
        endpoint_path = f"/v1/replicas/{request.action}"
        body = canonical_json(request.to_payload()).encode("utf-8")
        http_request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with self._opener.open(
                http_request,
                timeout=timeout_seconds,
            ) as response:
                raw_body = response.read()
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            is_timeout = exc.code == 504
            code = (
                ErrorCode.LAUNCH_TIMEOUT
                if is_timeout
                else ErrorCode.LAUNCH_HTTP_ERROR
            )
            raise BusinessError(
                code,
                "remote lifecycle endpoint returned an HTTP error",
                details={
                    "endpoint_path": endpoint_path,
                    "http_status": exc.code,
                    "replica_id": request.replica_id,
                    "action": request.action,
                },
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise BusinessError(
                    ErrorCode.LAUNCH_TIMEOUT,
                    "lifecycle action exceeded its single-action budget",
                    details={
                        "endpoint_path": endpoint_path,
                        "replica_id": request.replica_id,
                        "action": request.action,
                        "timeout_seconds": timeout_seconds,
                    },
                ) from exc
            if (
                isinstance(reason, ConnectionRefusedError)
                or getattr(reason, "errno", None) == errno.ECONNREFUSED
            ):
                raise BusinessError(
                    ErrorCode.LAUNCH_CONNECTION_REFUSED,
                    "lifecycle endpoint refused the connection",
                    details={
                        "endpoint_path": endpoint_path,
                        "replica_id": request.replica_id,
                        "action": request.action,
                    },
                ) from exc
            raise BusinessError(
                ErrorCode.LAUNCH_CONNECTION_REFUSED,
                "cannot establish a lifecycle connection",
                details={
                    "endpoint_path": endpoint_path,
                    "replica_id": request.replica_id,
                    "action": request.action,
                    "reason": str(reason),
                },
            ) from exc
        except TimeoutError as exc:
            raise BusinessError(
                ErrorCode.LAUNCH_TIMEOUT,
                "lifecycle action exceeded its single-action budget",
                details={
                    "endpoint_path": endpoint_path,
                    "replica_id": request.replica_id,
                    "action": request.action,
                    "timeout_seconds": timeout_seconds,
                },
            ) from exc

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                ErrorCode.LAUNCH_BAD_RESPONSE,
                "lifecycle endpoint returned invalid JSON",
                details={
                    "endpoint_path": endpoint_path,
                    "http_status": status,
                    "replica_id": request.replica_id,
                },
            ) from exc

        if not isinstance(payload, dict):
            raise BusinessError(
                ErrorCode.LAUNCH_BAD_RESPONSE,
                "lifecycle endpoint response must be a JSON object",
                details={
                    "endpoint_path": endpoint_path,
                    "replica_id": request.replica_id,
                },
            )
        if payload.get("ok") is not True:
            remote_code = str(payload.get("error_code", "REMOTE_REJECTED"))
            raise BusinessError(
                ErrorCode.LAUNCH_REMOTE_ERROR,
                "remote lifecycle endpoint rejected the action",
                details={
                    "endpoint_path": endpoint_path,
                    "replica_id": request.replica_id,
                    "action": request.action,
                    "remote_error_code": remote_code,
                },
            )

        remote_replica = payload.get("replica_id")
        if remote_replica != request.replica_id:
            raise BusinessError(
                ErrorCode.LAUNCH_BAD_RESPONSE,
                "lifecycle response echoed a different replica id",
                details={
                    "expected_replica_id": request.replica_id,
                    "actual_replica_id": remote_replica,
                },
            )
        echoed_version = payload.get("model_version")
        if echoed_version != request.model_version:
            raise BusinessError(
                ErrorCode.REPLICA_VERSION_MISMATCH,
                "lifecycle response echoed a different model version",
                details={
                    "replica_id": request.replica_id,
                    "expected_model_version": request.model_version,
                    "actual_model_version": echoed_version,
                },
            )
        echoed_digest = payload.get("sample_digest")
        if echoed_digest != request.sample_digest:
            raise BusinessError(
                ErrorCode.SAMPLE_DIGEST_MISMATCH,
                "lifecycle response echoed a different sample digest",
                details={
                    "replica_id": request.replica_id,
                    "expected_sample_digest": request.sample_digest,
                    "actual_sample_digest": echoed_digest,
                },
            )
        echoed_action = payload.get("action")
        if echoed_action is not None and echoed_action != request.action:
            raise BusinessError(
                ErrorCode.LAUNCH_BAD_RESPONSE,
                "lifecycle response echoed a different action",
                details={
                    "expected_action": request.action,
                    "actual_action": echoed_action,
                },
            )

        return {
            "http_status": status,
            "replica_id": remote_replica,
            "service_name": payload.get("service_name"),
            "model_version": echoed_version,
            "sample_digest": echoed_digest,
            "action": echoed_action or request.action,
        }

