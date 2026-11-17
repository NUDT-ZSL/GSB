"""Loopback TCP JSON-lines protocol and server/client implementations."""

from __future__ import annotations

import base64
import json
import socketserver
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    BlockError,
    GridError,
    IntegrityError,
    LedgerDomainError,
    ProtocolError,
    SpecError,
    StoredLedgerError,
)
from .node_store import NodeStorage
from .spec import ReplicaGroupSpec
from .util import JsonlEventLog, canonical_json_bytes, is_int


PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 32 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def _remote_error(payload: dict[str, Any]) -> LedgerDomainError:
    code = payload.get("code", "E_PROTOCOL")
    message = payload.get("message", "remote operation failed")
    field_path = payload.get("field_path", "protocol")
    node_id = payload.get("node_id", "*")
    block_id = payload.get("block_id", "*")
    details = payload.get("details", {})
    if not isinstance(details, dict):
        details = {"remote_details": details}
    if code == "E_BLOCK":
        return BlockError(message, field_path, node_id=node_id, block_id=block_id, details=details)
    if code == "E_INTEGRITY":
        return IntegrityError(
            message, field_path, node_id=node_id, block_id=block_id, details=details
        )
    if code == "E_GRID":
        return GridError(message, field_path, node_id=node_id, block_id=block_id, details=details)
    if code == "E_SPEC":
        return SpecError(message, field_path, node_id=node_id, block_id=block_id, details=details)
    if code == "E_LEDGER":
        return StoredLedgerError(message, field_path, details=details)
    return ProtocolError(message, field_path, node_id=node_id, block_id=block_id, details=details)


class NodeClient:
    def __init__(self, host: str, port: int, *, timeout: float = 10.0):
        if host not in LOOPBACK_HOSTS:
            raise ProtocolError("客户端只允许连接回环地址", "protocol.host")
        if not is_int(port) or port <= 0 or port > 65535:
            raise ProtocolError("端口必须在 1..65535", "protocol.port")
        self.host = "127.0.0.1" if host == "localhost" else host
        self.port = int(port)
        self.timeout = timeout
        self._socket: Any = None
        self._reader: Any = None
        self._writer: Any = None

    def __enter__(self) -> "NodeClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def connect(self) -> None:
        import socket

        if self._socket is not None:
            return
        try:
            connection = socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )
        except OSError as exc:
            raise ProtocolError(
                "无法连接本地回环节点",
                "protocol.connect",
                details={"host": self.host, "port": self.port, "os_error": str(exc)},
            ) from exc
        self._socket = connection
        self._reader = connection.makefile("rb")
        self._writer = connection.makefile("wb")

    def close(self) -> None:
        for stream in (self._reader, self._writer):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            if self._socket is not None:
                self._socket.close()
        except OSError:
            pass
        self._socket = None
        self._reader = None
        self._writer = None

    def request(self, op: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._socket is None:
            self.connect()
        request_id = uuid.uuid4().hex
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "op": op,
            "payload": payload or {},
        }
        try:
            self._writer.write(canonical_json_bytes(request) + b"\n")
            self._writer.flush()
            line = self._reader.readline(MAX_LINE_BYTES + 1)
        except OSError as exc:
            raise ProtocolError(
                "回环协议读写失败",
                "protocol.io",
                details={"op": op, "os_error": str(exc)},
            ) from exc
        if not line:
            raise ProtocolError("节点在响应前关闭了连接", "protocol.response")
        if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
            raise ProtocolError("响应帧超长或不完整", "protocol.response")
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(
                "响应不是合法 JSON", "protocol.response", details={"error": str(exc)}
            ) from exc
        if not isinstance(response, dict):
            raise ProtocolError("响应必须是 JSON 对象", "protocol.response")
        if response.get("request_id") != request_id:
            raise ProtocolError(
                "响应请求编号不匹配",
                "protocol.request_id",
                details={"expected": request_id, "actual": response.get("request_id")},
            )
        if not response.get("ok"):
            error = response.get("error")
            if isinstance(error, dict):
                raise _remote_error(error)
            raise ProtocolError("远端返回了未知错误结构", "protocol.error")
        data = response.get("data")
        if not isinstance(data, dict):
            raise ProtocolError("响应 data 必须是对象", "protocol.data")
        return data

    def ping(self) -> dict[str, Any]:
        return self.request("PING")

    def query_digest(
        self, block_id: str, shard_layout: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return self.request(
            "DIGEST_QUERY",
            {"block_id": block_id, "shard_layout": shard_layout},
        )

    def get_block(self, block_id: str) -> tuple[bytes, str, int]:
        data = self.request("BLOCK_GET", {"block_id": block_id})
        encoded = data.get("data_base64")
        digest = data.get("digest")
        byte_size = data.get("byte_size")
        if not isinstance(encoded, str) or not isinstance(digest, str):
            raise ProtocolError("BLOCK_GET 响应字段缺失", "protocol.data")
        try:
            payload = base64.b64decode(encoded.encode("ascii"), validate=True)
        except Exception as exc:
            raise ProtocolError("BLOCK_GET base64 损坏", "protocol.data_base64") from exc
        if not is_int(byte_size) or len(payload) != byte_size:
            raise ProtocolError("BLOCK_GET 长度与声明不一致", "protocol.byte_size")
        return payload, digest, int(byte_size)

    def put_block(
        self,
        block_id: str,
        data: bytes,
        *,
        expected_digest: str,
        expected_byte_size: int,
    ) -> dict[str, Any]:
        return self.request(
            "BLOCK_PUT",
            {
                "block_id": block_id,
                "data_base64": base64.b64encode(data).decode("ascii"),
                "expected_digest": expected_digest,
                "expected_byte_size": expected_byte_size,
            },
        )


class _ThreadingTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class NodeServer:
    def __init__(
        self,
        spec: ReplicaGroupSpec,
        node_id: str,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        log_path: str | Path | None = None,
    ) -> None:
        if host not in LOOPBACK_HOSTS:
            raise ProtocolError("节点只允许绑定回环地址", "protocol.host")
        if not is_int(port) or port < 0 or port > 65535:
            raise ProtocolError("端口必须在 0..65535", "protocol.port")
        node = spec.node_map.get(node_id)
        if node is None:
            raise ProtocolError(
                "未知节点标识", "protocol.node_id", node_id=node_id
            )
        self.spec = spec
        self.node = node
        self.host = "127.0.0.1" if host == "localhost" else host
        self.logger = JsonlEventLog(log_path)
        self.storage = NodeStorage(node, logger=self.logger)
        self.recovery = self.storage.recover()
        handler_class = _make_handler(self)
        self._server = _ThreadingTcpServer((self.host, int(port)), handler_class)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def endpoint(self) -> tuple[str, int]:
        return (self.host, self.port)

    def start(self) -> "NodeServer":
        if self._thread is not None:
            return self
        thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"replica-node-{self.node.node_id}",
            daemon=True,
        )
        thread.start()
        self._thread = thread
        self.logger.emit(
            "NODE_LISTENING",
            node_id=self.node.node_id,
            block_id="*",
            field_path="node.endpoint",
            context={"host": self.host, "port": self.port},
        )
        return self

    def stop(self) -> None:
        # No exit report is emitted.  Persistent files and journal records are
        # the only recovery inputs after either a clean or forced stop.
        if self._thread is not None:
            self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def __enter__(self) -> "NodeServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


def _make_handler(server: NodeServer) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            peer = self.client_address
            while True:
                line = self.rfile.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                    self._send_error(
                        request_id="",
                        error=ProtocolError("请求帧超长或不完整", "protocol.request"),
                    )
                    return
                request: Any = None
                try:
                    request = json.loads(line.decode("utf-8"))
                    if not isinstance(request, dict):
                        raise ProtocolError("请求必须是 JSON 对象", "protocol.request")
                    request_id = request.get("request_id")
                    if not isinstance(request_id, str) or not request_id:
                        raise ProtocolError("request_id 必须是非空字符串", "protocol.request_id")
                    if request.get("protocol_version") != PROTOCOL_VERSION:
                        raise ProtocolError(
                            "不支持的协议版本", "protocol.protocol_version"
                        )
                    op = request.get("op")
                    if not isinstance(op, str):
                        raise ProtocolError("op 必须是非空字符串", "protocol.op")
                    payload = request.get("payload", {})
                    if not isinstance(payload, dict):
                        raise ProtocolError("payload 必须是对象", "protocol.payload")
                    data = self._dispatch(op, payload, request_id, peer)
                    self._send_ok(request_id, data)
                except LedgerDomainError as exc:
                    server.logger.error(exc, event="PROTOCOL_REQUEST_FAILED")
                    request_id = ""
                    if isinstance(request, dict) and isinstance(request.get("request_id"), str):
                        request_id = request["request_id"]
                    self._send_error(request_id=request_id, error=exc)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    error = ProtocolError(
                        "请求不是合法 UTF-8 JSON",
                        "protocol.request",
                        details={"error": str(exc)},
                    )
                    server.logger.error(error, event="PROTOCOL_REQUEST_FAILED")
                    self._send_error(request_id="", error=error)
                except BaseException as exc:
                    error = ProtocolError(
                        "节点处理请求时发生内部错误",
                        "protocol.internal",
                        details={"error_type": type(exc).__name__, "error": str(exc)},
                    )
                    server.logger.error(error, event="PROTOCOL_INTERNAL_ERROR")
                    self._send_error(request_id="", error=error)

        def _dispatch(
            self,
            op: str,
            payload: dict[str, Any],
            request_id: str,
            peer: tuple[str, int],
        ) -> dict[str, Any]:
            block_id = payload.get("block_id", "*")
            if op == "PING":
                server.storage.append_protocol_record(
                    {"event": "REQUEST", "op": op, "request_id": request_id}
                )
                return {
                    "node_id": server.node.node_id,
                    "protocol_version": PROTOCOL_VERSION,
                    "persistent_blocks": list(server.storage.block_ids),
                }
            if op == "HELLO":
                server.storage.append_protocol_record(
                    {"event": "REQUEST", "op": op, "request_id": request_id}
                )
                return {
                    "node_id": server.node.node_id,
                    "protocol_version": PROTOCOL_VERSION,
                    "loopback": True,
                }
            if op not in {"DIGEST_QUERY", "BLOCK_GET", "BLOCK_PUT"}:
                raise ProtocolError("未知协议操作", "protocol.op")
            if not isinstance(block_id, str) or not block_id:
                raise ProtocolError("block_id 必须是非空字符串", "protocol.block_id")
            server.storage.append_protocol_record(
                {
                    "event": "REQUEST",
                    "op": op,
                    "request_id": request_id,
                    "block_id": block_id,
                    "peer": list(peer),
                }
            )
            if op == "DIGEST_QUERY":
                layout = payload.get("shard_layout")
                if layout is not None and not isinstance(layout, list):
                    raise ProtocolError("shard_layout 必须是数组", "protocol.shard_layout")
                return server.storage.query_digest(block_id, layout)
            if op == "BLOCK_GET":
                data, digest = server.storage.read_block(block_id)
                return {
                    "block_id": block_id,
                    "byte_size": len(data),
                    "digest": digest,
                    "data_base64": base64.b64encode(data).decode("ascii"),
                }
            encoded = payload.get("data_base64")
            expected_digest = payload.get("expected_digest")
            expected_byte_size = payload.get("expected_byte_size")
            if not isinstance(encoded, str):
                raise ProtocolError("data_base64 必须是字符串", "protocol.data_base64")
            if not isinstance(expected_digest, str):
                raise ProtocolError(
                    "expected_digest 必须是字符串", "protocol.expected_digest"
                )
            if not is_int(expected_byte_size):
                raise ProtocolError(
                    "expected_byte_size 必须是整数", "protocol.expected_byte_size"
                )
            try:
                data = base64.b64decode(encoded.encode("ascii"), validate=True)
            except Exception as exc:
                raise ProtocolError("data_base64 损坏", "protocol.data_base64") from exc
            return server.storage.put_block(
                block_id,
                data,
                expected_digest=expected_digest,
                expected_byte_size=int(expected_byte_size),
                request_id=request_id,
            )

        def _send_ok(self, request_id: str, data: dict[str, Any]) -> None:
            response = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "data": data,
            }
            self._write(response)

        def _send_error(self, *, request_id: str, error: LedgerDomainError) -> None:
            response = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": False,
                "error": error.to_dict(),
            }
            self._write(response)

        def _write(self, response: dict[str, Any]) -> None:
            try:
                self.wfile.write(canonical_json_bytes(response) + b"\n")
                self.wfile.flush()
            except OSError:
                return

    return Handler


def wait_for_port(host: str, port: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with NodeClient(host, port, timeout=0.5) as client:
                client.ping()
                return
        except (ProtocolError, LedgerDomainError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise ProtocolError(
        "等待节点端口就绪超时",
        "protocol.connect",
        details={"host": host, "port": port, "last_error": str(last_error)},
    )
