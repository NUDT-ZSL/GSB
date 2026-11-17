from __future__ import annotations

import errno
import json
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .errors import EdgeError, PortError, ProtocolError, error_payload
from .util import JsonLogger, canonical_json


MAX_LINE_BYTES = 4 * 1024 * 1024


def require_loopback_free(host: str, port: int) -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise PortError(
            "only loopback hosts are supported",
            location=f"{host}:{port}",
            host=host,
            port=port,
        )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
    except OSError as exc:
        raise PortError(
            "loopback port is already in use or unavailable",
            location=f"{host}:{port}",
            host=host,
            port=port,
            errno=exc.errno,
            os_error=exc.strerror,
        ) from exc
    finally:
        sock.close()


@dataclass
class RequestResult:
    payload: dict[str, Any]
    stop_after_response: bool = False


class JsonLineServer:
    def __init__(
        self,
        host: str,
        port: int,
        handler: Callable[[dict[str, Any]], dict[str, Any] | RequestResult],
        logger: JsonLogger,
        component: str,
    ) -> None:
        self.host = host
        self.port = port
        self.handler = handler
        self.logger = logger
        self.component = component
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.bound_port: int | None = None

    def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((self.host, self.port))
            sock.listen(64)
            sock.settimeout(0.5)
        except OSError as exc:
            sock.close()
            raise PortError(
                "failed to bind loopback TCP port; no alternate port will be tried",
                location=f"{self.host}:{self.port}",
                host=self.host,
                port=self.port,
                errno=exc.errno,
                os_error=exc.strerror,
            ) from exc
        self._sock = sock
        self.bound_port = int(sock.getsockname()[1])
        self.logger.info(
            "listening",
            host=self.host,
            port=self.bound_port,
            protocol="json-line/tcp",
        )
        return self.bound_port

    def request_stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def serve_forever(self) -> None:
        if self._sock is None:
            self.start()
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, address = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            thread = threading.Thread(
                target=self._client_loop,
                args=(client, address),
                daemon=True,
                name=f"{self.component}-client-{address}",
            )
            self._threads.append(thread)
            thread.start()

    def _client_loop(self, client: socket.socket, address: tuple[str, int]) -> None:
        client.settimeout(30)
        buffer = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = client.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_LINE_BYTES and b"\n" not in buffer:
                    self._send_error(client, ProtocolError("request line too large"), address)
                    break
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if not raw.strip():
                        continue
                    stop = self._dispatch(client, raw, address)
                    if stop:
                        return
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _dispatch(self, client: socket.socket, raw: bytes, address: tuple[str, int]) -> bool:
        try:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProtocolError(
                    "request is not valid UTF-8 JSON",
                    location=f"{self.host}:{self.bound_port}",
                    parser_error=str(exc),
                ) from exc
            if not isinstance(payload, dict):
                raise ProtocolError("request must be a JSON object")
            result = self.handler(payload)
            if isinstance(result, RequestResult):
                response = {"ok": True, **result.payload}
                stop = result.stop_after_response
            else:
                response = {"ok": True, **result}
                stop = False
        except Exception as exc:
            if isinstance(exc, EdgeError):
                self.logger.error(
                    "request_failed",
                    code=exc.code,
                    message=exc.message,
                    location=exc.location,
                    details=exc.details,
                    client=f"{address[0]}:{address[1]}",
                )
            else:
                self.logger.error(
                    "request_crashed",
                    code="E_INTERNAL",
                    message=str(exc),
                    exception=exc.__class__.__name__,
                    client=f"{address[0]}:{address[1]}",
                )
            response = {"ok": False, "error": error_payload(exc)}
            stop = False
        try:
            client.sendall((canonical_json(response) + "\n").encode("utf-8"))
        except OSError:
            return stop
        if stop:
            self.request_stop()
        return stop

    def _send_error(self, client: socket.socket, exc: EdgeError, address: tuple[str, int]) -> None:
        self.logger.error(
            "request_rejected",
            code=exc.code,
            message=exc.message,
            client=f"{address[0]}:{address[1]}",
        )
        try:
            client.sendall(
                (canonical_json({"ok": False, "error": exc.to_dict()}) + "\n").encode("utf-8")
            )
        except OSError:
            pass


def _error_from_payload(payload: Mapping[str, Any]) -> EdgeError:
    return EdgeError(
        code=str(payload.get("code", "E_PROTOCOL")),
        message=str(payload.get("message", "remote error")),
        location=payload.get("location"),
        details=dict(payload.get("details") or {}),
    )


def rpc(
    host: str,
    port: int,
    payload: Mapping[str, Any],
    timeout: float = 10.0,
) -> dict[str, Any]:
    request = (canonical_json(dict(payload)) + "\n").encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
    except OSError as exc:
        raise ProtocolError(
            "could not reach loopback service",
            location=f"{host}:{port}",
            errno=exc.errno,
            os_error=exc.strerror,
        ) from exc
    raw = b"".join(chunks)
    line = raw.split(b"\n", 1)[0]
    if not line:
        raise ProtocolError("empty response from loopback service", location=f"{host}:{port}")
    try:
        response = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(
            "response is not valid UTF-8 JSON",
            location=f"{host}:{port}",
            parser_error=str(exc),
        ) from exc
    if not isinstance(response, dict):
        raise ProtocolError("response must be a JSON object", location=f"{host}:{port}")
    if not response.get("ok", False):
        raise _error_from_payload(dict(response.get("error") or {}))
    return response


def wait_until_ready(
    host: str,
    port: int,
    *,
    timeout: float = 10.0,
    process_poll: Callable[[], int | None] | None = None,
) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process_poll is not None:
            code = process_poll()
            if code is not None:
                raise ProtocolError(
                    "process exited before becoming ready",
                    location=f"{host}:{port}",
                    exit_code=code,
                )
        try:
            return rpc(host, port, {"op": "ping"}, timeout=1.0)
        except EdgeError as exc:
            last_error = exc
            time.sleep(0.05)
    raise ProtocolError(
        "service did not become ready before timeout",
        location=f"{host}:{port}",
        last_error=error_payload(last_error) if last_error else None,
    )
