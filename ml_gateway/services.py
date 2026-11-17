from __future__ import annotations

import json
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from .canonical import canonical_json
from .declarations import parse_request_documents
from .errors import GatewayError, InputError, PortError, UpstreamError
from .inference import FingerprintMismatch, InferenceRuntime
from .models import BackendState, DeclarationBundle, Request


BACKEND_PROTOCOL = "mlgw-backend-v1"
GATEWAY_PROTOCOL = "mlgw-gateway-v1"


def _server_class(host: str) -> type[socketserver.ThreadingTCPServer]:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET

    class _Server(socketserver.ThreadingTCPServer):
        address_family = family
        allow_reuse_address = True
        daemon_threads = True

    return _Server


class _BackendHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for raw_line in self.rfile:
            if not raw_line.strip():
                continue
            try:
                request = json.loads(raw_line)
                if not isinstance(request, dict):
                    raise ValueError("backend request must be an object")
                if request.get("protocol") != BACKEND_PROTOCOL:
                    raise ValueError("unsupported backend protocol")
                batch_id = request.get("batch_id")
                expected_fingerprint = request.get("fingerprint")
                inputs = request.get("inputs")
                if not isinstance(batch_id, str) or not batch_id:
                    raise ValueError("batch_id is required")
                if not isinstance(expected_fingerprint, str):
                    raise ValueError("fingerprint is required")
                if not isinstance(inputs, list):
                    raise ValueError("inputs must be an array")
                result = self.server.runtime.compute(  # type: ignore[attr-defined]
                    batch_id=batch_id,
                    expected_fingerprint=expected_fingerprint,
                    inputs=inputs,
                )
                response = {"ok": True, "protocol": BACKEND_PROTOCOL, **result}
            except FingerprintMismatch as exc:
                response = {
                    "error": exc.to_dict(),
                    "ok": False,
                    "protocol": BACKEND_PROTOCOL,
                }
            except Exception as exc:
                response = {
                    "error": {"code": "E_UPSTREAM", "message": str(exc)},
                    "ok": False,
                    "protocol": BACKEND_PROTOCOL,
                }
            self.wfile.write((canonical_json(response) + "\n").encode("utf-8"))
            self.wfile.flush()


class BackendTCPService:
    def __init__(self, *, host: str, port: int, runtime: InferenceRuntime) -> None:
        self.host = host
        self.port = port
        self.runtime = runtime
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        server_class = _server_class(self.host)
        try:
            server = server_class((self.host, self.port), _BackendHandler)
        except OSError as exc:
            raise PortError(
                "cannot bind backend loopback port",
                host=self.host,
                port=self.port,
                os_error=str(exc),
            ) from exc
        server.runtime = self.runtime  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name=f"backend-{self.runtime.state.backend_id}",
            daemon=True,
        )
        self._thread.start()

    def reload(self, state: BackendState, parameters: tuple[float, ...]) -> None:
        self.runtime.reload(state, parameters)

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)


class BackendPool:
    def __init__(self, services: dict[str, BackendTCPService]) -> None:
        self.services = services

    @classmethod
    def from_states(
        cls,
        states: dict[str, BackendState],
        bundle: DeclarationBundle,
    ) -> "BackendPool":
        declarations = bundle.backend_by_id
        services: dict[str, BackendTCPService] = {}
        for backend_id in sorted(states):
            state = states[backend_id]
            decl = declarations[backend_id]
            runtime = InferenceRuntime(state, decl.parameters)
            services[backend_id] = BackendTCPService(
                host=state.host,
                port=state.port,
                runtime=runtime,
            )
        return cls(services)

    def start(self) -> None:
        started: list[BackendTCPService] = []
        try:
            for backend_id in sorted(self.services):
                service = self.services[backend_id]
                service.start()
                started.append(service)
        except Exception:
            for service in reversed(started):
                service.stop()
            raise

    def reload(self, states: dict[str, BackendState], bundle: DeclarationBundle) -> None:
        declarations = bundle.backend_by_id
        for backend_id in sorted(states):
            self.services[backend_id].reload(
                states[backend_id], declarations[backend_id].parameters
            )

    def stop(self) -> None:
        for backend_id in sorted(self.services, reverse=True):
            self.services[backend_id].stop()


class TcpInferenceClient:
    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def call_batch(
        self,
        *,
        state: BackendState,
        batch_id: str,
        inputs: list[list[float]],
    ) -> dict[str, Any]:
        request = {
            "batch_id": batch_id,
            "fingerprint": state.fingerprint,
            "inputs": inputs,
            "protocol": BACKEND_PROTOCOL,
        }
        try:
            with socket.create_connection(
                (state.host, state.port), timeout=self.timeout
            ) as connection:
                connection.settimeout(self.timeout)
                connection.sendall((canonical_json(request) + "\n").encode("utf-8"))
                chunks: list[bytes] = []
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
        except OSError as exc:
            raise UpstreamError(
                "backend is unreachable",
                host=state.host,
                port=state.port,
                backend_id=state.backend_id,
                os_error=str(exc),
            ) from exc
        data = b"".join(chunks).splitlines()
        if not data:
            raise UpstreamError(
                "backend returned no response",
                host=state.host,
                port=state.port,
                backend_id=state.backend_id,
            )
        try:
            response = json.loads(data[0])
        except json.JSONDecodeError as exc:
            raise UpstreamError(
                "backend returned invalid JSON",
                backend_id=state.backend_id,
                os_error=str(exc),
            ) from exc
        if not response.get("ok"):
            error = response.get("error") or {}
            if error.get("code") == "E_FINGERPRINT":
                raise FingerprintMismatch(
                    str(error.get("message", "fingerprint mismatch")),
                    backend_id=state.backend_id,
                )
            raise UpstreamError(
                "backend rejected batch",
                backend_id=state.backend_id,
                backend_error=error,
            )
        duration = response.get("duration_ms")
        summary = response.get("output_summary")
        if not isinstance(duration, int) or not isinstance(summary, str):
            raise UpstreamError(
                "backend response is missing deterministic output fields",
                backend_id=state.backend_id,
            )
        return response


class _GatewayHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw_documents: list[Any] = []
        for line_number, raw_line in enumerate(self.rfile, start=1):
            if not raw_line.strip():
                continue
            try:
                raw_documents.append(json.loads(raw_line))
            except json.JSONDecodeError as exc:
                error = InputError(
                    f"invalid request JSON: {exc.msg}",
                    f"$[{line_number - 1}]",
                    line=line_number,
                )
                self.wfile.write(
                    (canonical_json({"error": error.to_dict(), "protocol": GATEWAY_PROTOCOL, "status": "E_INPUT"}) + "\n").encode("utf-8")
                )
                self.wfile.flush()
                return
        engine = self.server.engine  # type: ignore[attr-defined]
        try:
            requests = parse_request_documents(raw_documents, engine.bundle.feature_schema)
            report = engine.execute_requests(requests)
            output: Iterable[dict[str, Any]] = report.responses
        except GatewayError as exc:
            output = [
                {
                    "error": exc.to_dict(),
                    "protocol": GATEWAY_PROTOCOL,
                    "status": exc.code,
                }
            ]
        for response in output:
            self.wfile.write((canonical_json(response) + "\n").encode("utf-8"))
        self.wfile.flush()


class GatewayTCPServer:
    def __init__(self, *, host: str, port: int, engine: Any) -> None:
        self.host = host
        self.port = port
        self.engine = engine
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        server_class = _server_class(self.host)
        try:
            server = server_class((self.host, self.port), _GatewayHandler)
        except OSError as exc:
            raise PortError(
                "cannot bind gateway loopback port",
                host=self.host,
                port=self.port,
                os_error=str(exc),
            ) from exc
        server.engine = self.engine  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="gateway-jsonl", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)


def ensure_ports_available(host: str, ports: Iterable[int]) -> None:
    for port in sorted(set(ports)):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        except OSError as exc:
            raise PortError(
                "loopback port is already in use",
                host=host,
                port=port,
                os_error=str(exc),
            ) from exc
        finally:
            sock.close()


def send_gateway_requests(
    *,
    host: str,
    port: int,
    requests: list[Request],
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    payload = "".join(canonical_json(request.to_dict()) + "\n" for request in requests)
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall(payload.encode("utf-8"))
            connection.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as exc:
        raise UpstreamError(
            "gateway is unreachable", host=host, port=port, os_error=str(exc)
        ) from exc
    responses: list[dict[str, Any]] = []
    for line in b"".join(chunks).splitlines():
        if line.strip():
            responses.append(json.loads(line))
    return responses
