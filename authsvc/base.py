"""两版服务共享的 HTTP 处理骨架。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from .constants import HEALTH_PATH, QUERY_PATH


class JsonRequestHandler(BaseHTTPRequestHandler):
    """只负责 HTTP 解析与 JSON 输出；鉴权逻辑由子类实现。"""

    server_version = "authsvc-local"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # 探测过程不应向 stderr 输出请求日志。
        return

    # ---------------------------------------------------------------- routing
    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path == HEALTH_PATH:
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "authsvc-local",
                    "version": self.service_version_label(),
                },
            )
            return
        self._not_found()

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != QUERY_PATH:
            self._not_found()
            return
        # 只支持 Content-Length 描述的定长请求体；拒绝 chunked 以简化解析。
        if self.headers.get("Transfer-Encoding"):
            self._send_json(400, self.bad_request_body())
            return
        try:
            declared_length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self._send_json(400, self.bad_request_body())
            return
        if declared_length < 0:
            self._send_json(400, self.bad_request_body())
            return
        self.handle_query_body(declared_length)

    # ------------------------------------------------------------ hooks
    def service_version_label(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def handle_query_body(self, declared_length: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def bad_request_body(self) -> dict:
        return {
            "ok": False,
            "error": "bad_request",
            "message": "request rejected",
        }

    def unauthorized_body(self) -> dict:
        return {
            "ok": False,
            "error": "unauthorized",
            "message": "missing or invalid shared token",
        }

    def not_found_body(self) -> dict:
        return {
            "ok": False,
            "error": "not_found",
            "message": "account not found",
        }

    def too_large_body(self) -> dict:
        return {
            "ok": False,
            "error": "payload_too_large",
            "message": "request body exceeds the local limit",
        }

    # ---------------------------------------------------------------- helpers
    def _read_body(self, declared_length: int) -> bytes:
        return self.rfile.read(declared_length)

    def _discard_body(self, declared_length: int, cap: int = 1_048_576) -> None:
        """在返回错误响应前尽量读完请求体。

        Windows 的 TCP 栈在仍有未读入站数据时关闭连接会发送 RST，
        客户端可能因此看不到 413 响应。读取（或丢弃）请求体后
        再写响应，客户端才能稳定收到错误码。超大请求最多丢弃 ``cap``
        字节，避免被恶意请求拖住。
        """
        remaining = min(declared_length, cap)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _parse_json(self, raw_body: bytes) -> object:
        return json.loads(raw_body.decode("utf-8"))

    def _not_found(self) -> None:
        self._send_json(404, self.not_found_body())

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True
