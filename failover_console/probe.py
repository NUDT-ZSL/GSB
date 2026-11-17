"""Budgeted parallel health probes and loopback HTTP transport."""

from __future__ import annotations

import re
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

from .models import (
    ProbeAttempt,
    ProbeBatch,
    ProbeConfig,
    ProbeLimits,
    ProbeResult,
    InstanceSpec,
)

SUCCESS = "success"
UNREACHABLE = "unreachable"
TIMEOUT = "timeout"
ERROR = "error"
FAILURE_CONCLUSIONS = {UNREACHABLE, TIMEOUT, ERROR}


@dataclass(frozen=True)
class ProbeObservation:
    conclusion: str
    detail: str


class ProbeTransport(Protocol):
    def probe(self, port: int, timeout_ms: int) -> ProbeObservation: ...


class RealProbeTransport:
    """Minimal HTTP/1.1 health probe restricted to loopback callers."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host

    def probe(self, port: int, timeout_ms: int) -> ProbeObservation:
        timeout_s = timeout_ms / 1000.0
        request = (
            "GET /health HTTP/1.1\r\n"
            f"Host: {self.host}:{port}\r\n"
            "Connection: close\r\n"
            "User-Agent: failover-console/1.0\r\n\r\n"
        ).encode("ascii")
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((self.host, port), timeout=timeout_s)
            sock.settimeout(timeout_s)
            sock.sendall(request)
            chunks: list[bytes] = []
            total = 0
            while total < 8192:
                chunk = sock.recv(min(2048, 8192 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if b"\r\n\r\n" in b"".join(chunks):
                    break
            payload = b"".join(chunks)
            if not payload:
                return ProbeObservation(ERROR, "empty response")
            match = re.match(rb"HTTP/\d(?:\.\d)?\s+(\d{3})", payload)
            if not match:
                return ProbeObservation(ERROR, "malformed HTTP response")
            status = int(match.group(1))
            if 200 <= status <= 299:
                return ProbeObservation(SUCCESS, f"HTTP {status}")
            return ProbeObservation(ERROR, f"HTTP {status}")
        except (socket.timeout, TimeoutError):
            return ProbeObservation(TIMEOUT, f"timed out after {timeout_ms} ms")
        except (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            return ProbeObservation(UNREACHABLE, f"{type(exc).__name__}: {exc}")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


AttemptCallback = Callable[[ProbeAttempt], None]
FailureCallback = Callable[[ProbeResult], None]


class ProbeRunner:
    def __init__(
        self,
        transport: ProbeTransport | None = None,
        *,
        monotonic_ms: Callable[[], int] | None = None,
        epoch_ms: Callable[[], int] | None = None,
        on_attempt: AttemptCallback | None = None,
        on_exhausted: FailureCallback | None = None,
    ) -> None:
        self.transport = transport or RealProbeTransport()
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self.epoch_ms = epoch_ms or (lambda: int(time.time() * 1000))
        self.on_attempt = on_attempt
        self.on_exhausted = on_exhausted

    def run(
        self,
        instances: Iterable[InstanceSpec],
        limits: ProbeLimits,
        faults: dict[str, str],
        *,
        run_id: str,
        round_no: int,
    ) -> ProbeBatch:
        ordered = self._fair_order(list(instances))
        if not ordered:
            return ProbeBatch((), 0, {})

        global_semaphore = threading.Semaphore(limits.global_concurrency)
        service_semaphores = {
            service_id: threading.Semaphore(limits.service_quota)
            for service_id in sorted({item.service_id for item in ordered})
        }
        metric_lock = threading.Lock()
        active_global = 0
        active_by_service = {service_id: 0 for service_id in service_semaphores}
        peak_global = 0
        peak_by_service = {service_id: 0 for service_id in service_semaphores}

        def probe_one(instance: InstanceSpec) -> ProbeResult:
            nonlocal active_global, peak_global
            with global_semaphore:
                with service_semaphores[instance.service_id]:
                    with metric_lock:
                        active_global += 1
                        active_by_service[instance.service_id] += 1
                        peak_global = max(peak_global, active_global)
                        peak_by_service[instance.service_id] = max(
                            peak_by_service[instance.service_id],
                            active_by_service[instance.service_id],
                        )
                    try:
                        return self._probe_with_retries(
                            instance, faults.get(instance.instance_id), run_id, round_no
                        )
                    finally:
                        with metric_lock:
                            active_global -= 1
                            active_by_service[instance.service_id] -= 1

        results: list[ProbeResult] = []
        with ThreadPoolExecutor(
            max_workers=limits.global_concurrency,
            thread_name_prefix="health-probe",
        ) as executor:
            future_map: dict[Future[ProbeResult], InstanceSpec] = {
                executor.submit(probe_one, instance): instance for instance in ordered
            }
            for future in as_completed(future_map):
                results.append(future.result())
        results.sort(key=lambda item: (item.service_id, item.instance_id))
        return ProbeBatch(
            results=tuple(results),
            peak_global=peak_global,
            peak_by_service=peak_by_service,
        )

    def _probe_with_retries(
        self,
        instance: InstanceSpec,
        fault_kind: str | None,
        run_id: str,
        round_no: int,
    ) -> ProbeResult:
        attempts: list[ProbeAttempt] = []
        for attempt_no in range(1, instance.probe.retries + 2):
            started = self.monotonic_ms()
            observation = self._perform_observation(instance.port, instance.probe, fault_kind)
            ended = self.monotonic_ms()
            attempt = ProbeAttempt(
                run_id=run_id,
                round_no=round_no,
                service_id=instance.service_id,
                instance_id=instance.instance_id,
                attempt_no=attempt_no,
                conclusion=observation.conclusion,
                detail=observation.detail,
                started_mono_ms=started,
                ended_mono_ms=ended,
                created_epoch_ms=self.epoch_ms(),
                fault_kind=fault_kind,
            )
            attempts.append(attempt)
            if self.on_attempt is not None:
                self.on_attempt(attempt)
            if observation.conclusion == SUCCESS:
                break
        result = ProbeResult(
            service_id=instance.service_id,
            instance_id=instance.instance_id,
            attempts=tuple(attempts),
            final_conclusion=attempts[-1].conclusion,
        )
        if not result.success and self.on_exhausted is not None:
            self.on_exhausted(result)
        return result

    def _perform_observation(
        self, port: int, probe: ProbeConfig, fault_kind: str | None
    ) -> ProbeObservation:
        if fault_kind == UNREACHABLE:
            return ProbeObservation(UNREACHABLE, "injected unreachable")
        if fault_kind == TIMEOUT:
            time.sleep(probe.timeout_ms / 1000.0)
            return ProbeObservation(TIMEOUT, f"injected timeout after {probe.timeout_ms} ms")
        if fault_kind == ERROR:
            return ProbeObservation(ERROR, "injected HTTP 503")
        try:
            return self.transport.probe(port, probe.timeout_ms)
        except (socket.timeout, TimeoutError):
            return ProbeObservation(TIMEOUT, f"timed out after {probe.timeout_ms} ms")
        except (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            return ProbeObservation(UNREACHABLE, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # transport defects are recorded, not hidden
            return ProbeObservation(ERROR, f"transport error: {type(exc).__name__}: {exc}")

    @staticmethod
    def _fair_order(instances: list[InstanceSpec]) -> list[InstanceSpec]:
        by_service: dict[str, list[InstanceSpec]] = {}
        for instance in instances:
            by_service.setdefault(instance.service_id, []).append(instance)
        for service_id in by_service:
            by_service[service_id].sort(key=lambda item: item.instance_id)
        ordered: list[InstanceSpec] = []
        depth = 0
        while True:
            added = False
            for service_id in sorted(by_service):
                bucket = by_service[service_id]
                if depth < len(bucket):
                    ordered.append(bucket[depth])
                    added = True
            if not added:
                return ordered
            depth += 1
