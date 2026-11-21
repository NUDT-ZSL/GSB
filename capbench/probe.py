from __future__ import annotations

import json
from typing import Protocol, Sequence
from urllib.parse import quote, urlencode

import httpx

from .scenario import GradientSpec, Service
from .simulator import simulate_response
from .workload import PlannedRequest, RequestOutcome


class Probe(Protocol):
    def probe_window(
        self,
        *,
        service: Service,
        tenant: str,
        gradient: GradientSpec,
        window_index: int,
        requests: Sequence[PlannedRequest],
        expected_status: int,
        timeout_ms: int,
        seed: int,
        run_id: str,
    ) -> list[RequestOutcome]:
        ...


def _outcome_from_payload(
    *,
    status: int,
    latency_ms: float | None,
    payload: dict | None,
    expected_status: int,
    timeout_ms: int,
    request: PlannedRequest,
    gradient: GradientSpec,
    window_index: int,
    tenant: str,
) -> RequestOutcome:
    marker = payload.get("outcome") if payload else None
    if marker == "connection_refused":
        return RequestOutcome(
            status=status,
            latency_ms=None,
            category="connection_refused",
            detail="simulated TCP connection refusal",
        )
    if marker == "interrupted":
        return RequestOutcome(
            status=status,
            latency_ms=latency_ms,
            category="target_service_interrupted",
            detail=f"target fault marker at status {status}",
        )
    if marker == "malformed" or (payload is not None and payload.get("malformed")):
        return RequestOutcome(
            status=status,
            latency_ms=latency_ms,
            category="malformed_response",
            detail="target returned a malformed response marker",
        )
    if payload is not None:
        identity_errors = []
        if payload.get("tenant") != tenant:
            identity_errors.append("tenant")
        if payload.get("gradient_index") != gradient.index:
            identity_errors.append("gradient_index")
        if payload.get("window_index") != window_index:
            identity_errors.append("window_index")
        if payload.get("request_seq") != request.sequence:
            identity_errors.append("request_seq")
        if identity_errors:
            return RequestOutcome(
                status=status,
                latency_ms=latency_ms,
                category="malformed_response",
                detail="response identity mismatch: " + ",".join(identity_errors),
            )
    if latency_ms is None:
        return RequestOutcome(
            status=status,
            latency_ms=None,
            category="malformed_response",
            detail="missing simulated_latency_ms",
        )
    if latency_ms > timeout_ms:
        return RequestOutcome(
            status=status,
            latency_ms=latency_ms,
            category="request_timeout",
            detail=f"logical latency {latency_ms:g}ms > budget {timeout_ms}ms",
        )
    if status != expected_status:
        return RequestOutcome(
            status=status,
            latency_ms=latency_ms,
            category="status_mismatch",
            detail=f"expected {expected_status}, received {status}",
        )
    return RequestOutcome(
        status=status,
        latency_ms=latency_ms,
        category=None,
        detail=None,
    )


class HttpProbe:
    """Actual HTTP probe against the local programmable target service."""

    def __init__(self, *, trust_env: bool = False) -> None:
        self.trust_env = trust_env

    def probe_window(
        self,
        *,
        service: Service,
        tenant: str,
        gradient: GradientSpec,
        window_index: int,
        requests: Sequence[PlannedRequest],
        expected_status: int,
        timeout_ms: int,
        seed: int,
        run_id: str,
    ) -> list[RequestOutcome]:
        outcomes: list[RequestOutcome] = []
        timeout_seconds = max(0.001, timeout_ms / 1000.0)
        limits = httpx.Limits(
            max_keepalive_connections=max(4, gradient.concurrency),
            max_connections=max(4, gradient.concurrency),
        )
        with httpx.Client(
            timeout=timeout_seconds,
            trust_env=self.trust_env,
            limits=limits,
            follow_redirects=False,
        ) as client:
            for request in requests:
                outcomes.append(
                    self._probe_one(
                        client=client,
                        service=service,
                        tenant=tenant,
                        gradient=gradient,
                        window_index=window_index,
                        request=request,
                        expected_status=expected_status,
                        timeout_ms=timeout_ms,
                        seed=seed,
                        run_id=run_id,
                    )
                )
        return outcomes

    def _probe_one(
        self,
        *,
        client: httpx.Client,
        service: Service,
        tenant: str,
        gradient: GradientSpec,
        window_index: int,
        request: PlannedRequest,
        expected_status: int,
        timeout_ms: int,
        seed: int,
        run_id: str,
    ) -> RequestOutcome:
        query = urlencode(
            {
                "concurrency": gradient.concurrency,
                "seed": seed,
                "run_id": run_id,
            }
        )
        path = (
            f"/v1/tenants/{quote(tenant, safe='')}"
            f"/gradients/{gradient.index}"
            f"/windows/{window_index}"
            f"/requests/{request.sequence}?{query}"
        )
        host = service.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        url = f"http://{host}:{service.port}{path}"
        try:
            response = client.get(url)
        except httpx.ConnectError:
            return RequestOutcome(
                category="connection_refused",
                detail="TCP connection refused or unreachable",
            )
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            return RequestOutcome(
                category="request_timeout",
                detail="HTTP transport timeout",
            )
        except httpx.RemoteProtocolError:
            return RequestOutcome(
                category="connection_refused",
                detail="target closed the connection before sending a response",
            )
        except (httpx.ReadError, httpx.TransportError):
            return RequestOutcome(
                category="target_service_interrupted",
                detail="target transport interrupted",
            )
        header_latency = response.headers.get("x-capbench-latency-ms")
        latency_ms: float | None = None
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                payload = None
        except (json.JSONDecodeError, ValueError):
            payload = None

        raw_latency = header_latency
        if raw_latency is None and payload is not None:
            raw_latency = payload.get("simulated_latency_ms")
        if raw_latency is not None:
            try:
                latency_ms = float(raw_latency)
            except (TypeError, ValueError):
                latency_ms = None
        if payload is None:
            return RequestOutcome(
                status=response.status_code,
                latency_ms=latency_ms,
                category="malformed_response",
                detail="response body is not a JSON object",
            )
        body_latency = payload.get("simulated_latency_ms")
        if body_latency is not None:
            try:
                if latency_ms is not None and abs(float(body_latency) - latency_ms) > 1e-6:
                    return RequestOutcome(
                        status=response.status_code,
                        latency_ms=latency_ms,
                        category="malformed_response",
                        detail="latency header/body mismatch",
                    )
            except (TypeError, ValueError):
                return RequestOutcome(
                    status=response.status_code,
                    latency_ms=latency_ms,
                    category="malformed_response",
                    detail="invalid simulated_latency_ms",
                )

        return _outcome_from_payload(
            status=response.status_code,
            latency_ms=latency_ms,
            payload=payload,
            expected_status=expected_status,
            timeout_ms=timeout_ms,
            request=request,
            gradient=gradient,
            window_index=window_index,
            tenant=tenant,
        )


class SimulatedProbe:
    """In-process probe for deterministic tests and offline demos."""

    def probe_window(
        self,
        *,
        service: Service,
        tenant: str,
        gradient: GradientSpec,
        window_index: int,
        requests: Sequence[PlannedRequest],
        expected_status: int,
        timeout_ms: int,
        seed: int,
        run_id: str,
    ) -> list[RequestOutcome]:
        tenant_config = next(item for item in service.tenants if item.name == tenant)
        outcomes: list[RequestOutcome] = []
        for request in requests:
            payload = simulate_response(
                profile=tenant_config.profile,
                seed=seed,
                tenant=tenant,
                gradient_index=gradient.index,
                window_index=window_index,
                request_seq=request.sequence,
                concurrency=gradient.concurrency,
            )
            latency = payload["simulated_latency_ms"]
            outcome = _outcome_from_payload(
                status=payload["status"],
                latency_ms=latency,
                payload=payload,
                expected_status=expected_status,
                timeout_ms=timeout_ms,
                request=request,
                gradient=gradient,
                window_index=window_index,
                tenant=tenant,
            )
            outcomes.append(outcome)
        return outcomes

