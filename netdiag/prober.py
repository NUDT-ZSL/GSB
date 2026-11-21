from __future__ import annotations

import http.client
import json
import socket
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import ProbeObservation, ProbeSpec, TargetSpec


@dataclass(frozen=True)
class HttpProber:
    """Probe a local loopback HTTP endpoint using bounded real waits."""

    real_timeout_cap_ms: int = 1000

    def probe(self, target: TargetSpec, probe: ProbeSpec) -> ProbeObservation:
        parsed = urlsplit(target.endpoint)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["netdiag_seq"] = str(probe.seq)
        path = urlunsplit(
            ("", "", parsed.path or "/", urlencode(sorted(query.items())), "")
        )
        host = parsed.hostname or ""
        port = parsed.port or 80
        real_timeout_s = min(
            max(target.timeout_ms, 1), self.real_timeout_cap_ms
        ) / 1000.0
        connection = http.client.HTTPConnection(host, port, timeout=real_timeout_s)
        started = time.monotonic()
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": "local-netdiag/1",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            status = response.status
            headers = {key.lower(): value for key, value in response.getheaders()}
            body_bytes = response.read()
            wall_ms = max(1, int(round((time.monotonic() - started) * 1000)))
            logical_latency = self._logical_latency(headers, wall_ms)
            if logical_latency > target.timeout_ms:
                return ProbeObservation(
                    outcome="timeout",
                    logical_duration_ms=target.timeout_ms,
                    response_status=status,
                    observed_latency_ms=logical_latency,
                    detail=(
                        "simulated response latency exceeded the per-probe timeout"
                    ),
                )
            if status != target.expected_status:
                return ProbeObservation(
                    outcome="status_mismatch",
                    logical_duration_ms=logical_latency,
                    response_status=status,
                    observed_latency_ms=logical_latency,
                    detail=(
                        f"expected HTTP {target.expected_status}, received HTTP {status}"
                    ),
                )
            try:
                json.loads(body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return ProbeObservation(
                    outcome="malformed_response",
                    logical_duration_ms=logical_latency,
                    response_status=status,
                    observed_latency_ms=logical_latency,
                    detail="response body is not valid UTF-8 JSON",
                )
            return ProbeObservation(
                outcome="success",
                logical_duration_ms=logical_latency,
                response_status=status,
                observed_latency_ms=logical_latency,
                detail="HTTP response matched status and JSON body requirements",
            )
        except (socket.timeout, TimeoutError):
            return ProbeObservation(
                outcome="timeout",
                logical_duration_ms=target.timeout_ms,
                response_status=None,
                observed_latency_ms=target.timeout_ms,
                detail="real HTTP wait exceeded per-probe timeout",
            )
        except (
            ConnectionRefusedError,
            ConnectionResetError,
            BrokenPipeError,
            http.client.RemoteDisconnected,
            http.client.IncompleteRead,
        ) as exc:
            return ProbeObservation(
                outcome="connection_refused",
                logical_duration_ms=1,
                response_status=None,
                detail=f"connection refused or disconnected early: {type(exc).__name__}",
            )
        except OSError as exc:
            return ProbeObservation(
                outcome="connection_refused",
                logical_duration_ms=1,
                response_status=None,
                detail=f"transport error: {type(exc).__name__}: {exc}",
            )
        finally:
            connection.close()

    @staticmethod
    def _logical_latency(headers: dict[str, str], wall_ms: int) -> int:
        raw = headers.get("x-netdiag-sim-latency-ms")
        if raw is None:
            return wall_ms
        try:
            return max(1, int(raw))
        except ValueError:
            return wall_ms
