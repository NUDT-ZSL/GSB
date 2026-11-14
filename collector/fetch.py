"""本地站点访问层。

采集对象是本地授权镜像站点快照，绝不访问公网。为真实地复现「页面暂时
不可读」「路径无响应」等镜像常见故障，程序在 127.0.0.1 上临时启动一个
只读 HTTP 服务，把站点目录作为文档根：

  - 普通路径：直接返回磁盘上的快照文件；
  - ``.mirror.json`` 中 ``unreadable`` 列出的路径：返回 HTTP 503；
  - ``hang`` 列出的路径：悬挂 ``hang_seconds`` 秒再返回（客户端先超时）。

所有请求都带 ``timeout``，并且受一次运行的总时限约束。服务只在本地回环
监听，进程退出即关闭。
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

REASON_UNREADABLE = "unreadable"
REASON_NOT_FOUND = "not_found"
REASON_BAD_PATH = "bad_path"


class FetchTimeout(Exception):
    """请求在限定时间内未返回。"""


class FetchError(Exception):
    """请求失败（不可读、不存在、越界等），``reason`` 为原因类别代码。"""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Page:
    url: str
    html: str


def _load_mirror(site_root: Path) -> tuple[set[str], set[str], float]:
    path = site_root / ".mirror.json"
    if not path.is_file():
        return set(), set(), 60.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set(), set(), 60.0
    unreadable = {p for p in data.get("unreadable", []) if isinstance(p, str)}
    hang = {p for p in data.get("hang", []) if isinstance(p, str)}
    seconds = data.get("hang_seconds", 60)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        seconds = 60.0
    return unreadable, hang, float(seconds)


class _Handler(http.server.BaseHTTPRequestHandler):
    site_root: Path
    unreadable: set[str]
    hang: set[str]
    hang_seconds: float

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # 静默，保持输出可预期

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path in self.unreadable:
            self._plain(503, "service unavailable")
            return
        if path in self.hang:
            time.sleep(self.hang_seconds)
            self._plain(504, "gateway timeout")
            return
        target = self._resolve(path)
        if target is None:
            self._plain(404, "not found")
            return
        try:
            data = target.read_bytes()
        except OSError:
            self._plain(500, "read error")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _plain(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _resolve(self, url_path: str) -> Path | None:
        rel = unquote(url_path).lstrip("/")
        if not rel or "\x00" in rel:
            return None
        candidate = (self.site_root / rel).resolve()
        try:
            candidate.relative_to(self.site_root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None


class SiteFetcher:
    """读取本地站点页面，支持不可读 / 无响应模拟与总时限。"""

    def __init__(
        self,
        site_root: Path,
        *,
        timeout_seconds: float,
        deadline_seconds: float,
    ):
        self.site_root = site_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.deadline_seconds = deadline_seconds
        self.unreadable, self.hang, self.hang_seconds = _load_mirror(self.site_root)
        self.deadline_at = time.monotonic() + deadline_seconds
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int | None = None
        self._session: requests.Session | None = None

    # -- 生命周期 ---------------------------------------------------------

    def __enter__(self) -> "SiteFetcher":
        self._start_server()
        self._session = requests.Session()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _start_server(self) -> None:
        if self._thread is not None:
            return
        handler = type(
            "_MirrorHandler",
            (_Handler,),
            {
                "site_root": self.site_root,
                "unreadable": self.unreadable,
                "hang": self.hang,
                "hang_seconds": self.hang_seconds,
            },
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        self._server = server
        self._port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._thread = None

    @property
    def origin(self) -> str:
        if self._port is None:
            raise RuntimeError("站点服务尚未启动")
        return f"http://127.0.0.1:{self._port}"

    # -- 读取 -------------------------------------------------------------

    def remaining(self) -> float:
        return self.deadline_at - time.monotonic()

    def fetch(self, site_path: str) -> Page:
        """读取站点内页面；``site_path`` 如 ``/announcements/page-1.html``。"""
        url = self._normalize(site_path)
        timeout = min(self.remaining(), self.timeout_seconds)
        if timeout <= 0:
            raise FetchTimeout("已达到总采集时限")

        assert self._session is not None
        try:
            response = self._session.get(
                self.origin + url,
                timeout=timeout,
                allow_redirects=False,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
        except requests.Timeout:
            raise FetchTimeout(f"请求超过 {timeout:g} 秒未返回") from None
        except requests.ConnectionError as exc:
            if isinstance(exc.args[0], socket.timeout):  # 某些平台的超时表现
                raise FetchTimeout(f"请求超过 {timeout:g} 秒未返回") from None
            raise FetchError(REASON_NOT_FOUND, f"无法连接本地站点服务：{exc}") from None
        except requests.RequestException as exc:
            raise FetchError(REASON_NOT_FOUND, f"请求失败：{exc}") from None

        if response.status_code == 503:
            raise FetchError(REASON_UNREADABLE, "来源暂时不可读（HTTP 503）")
        if response.status_code == 404:
            raise FetchError(REASON_NOT_FOUND, "页面不存在（HTTP 404）")
        if response.status_code != 200:
            raise FetchError(
                REASON_UNREADABLE, f"来源暂时不可读（HTTP {response.status_code}）"
            )
        response.encoding = "utf-8"
        return Page(url=url, html=response.text)

    def _normalize(self, site_path: str) -> str:
        parts = urlsplit(site_path)
        if parts.scheme or parts.netloc:
            if f"{parts.scheme}://{parts.netloc}" != self.origin:
                raise FetchError(REASON_BAD_PATH, "链接指向站点之外，已拒绝访问")
            return parts.path or "/"
        path = parts.path or "/"
        if not path.startswith("/"):
            path = "/" + path
        if parts.query:
            path += "?" + parts.query
        return path
