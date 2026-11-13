"""受限 HTTP 客户端：只允许访问本机目标，并记录全部请求。

对应验收：
  * 第 2 条：所有请求只指向 127.0.0.1 / localhost；
  * 第 11 条：非本机地址直接拒绝执行。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlencode, urlunparse

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
ALLOWED_SCHEMES = {"http", "https"}


class NonLocalTargetError(ValueError):
    """目标地址不是本机地址。"""


def assert_local_target(url: str) -> str:
    """校验目标 URL 必须是本机地址；非法/非本机一律抛 ValueError。"""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"目标地址格式非法（仅支持 http/https）: {url!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"目标地址格式非法，缺少主机名: {url!r}")

    host_lower = host.lower()
    # 显式主机名白名单
    if host_lower in ALLOWED_HOSTS:
        return host_lower
    # 显式回环 IP（含 127.0.0.0/8 整段与 ::1）
    try:
        ip = ipaddress.ip_address(host_lower)
        if ip.is_loopback:
            return host_lower
    except ValueError:
        pass
    raise NonLocalTargetError("仅支持本地目标")


def build_url(base_url: str, path: str, params: dict[str, str]) -> str:
    """在已校验为本机的 base_url 上拼接查询串。"""
    parsed = urlparse(base_url)
    query = urlencode(params)
    return urlunparse(
        (parsed.scheme, parsed.netloc, path, parsed.params, query, "")
    )


@dataclass
class RequestRecord:
    method: str
    url: str
    host: str
    param: str
    payload_category: str
    payload: str
    status_code: int | None
    error: str | None = None


@dataclass
class HttpResponse:
    status_code: int
    body: str
    error: str | None = None


class LocalHttpClient:
    """仅能访问本机的 HTTP 客户端，自动留存请求记录。"""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self.records: list[RequestRecord] = []

    def get(
        self,
        url: str,
        *,
        param: str,
        payload_category: str,
        payload: str,
    ) -> HttpResponse:
        # 每次请求前再次确认主机为本机，防止拼接过程引入外域
        host = assert_local_target(url)
        record = RequestRecord(
            method="GET",
            url=url,
            host=host,
            param=param,
            payload_category=payload_category,
            payload=payload,
            status_code=None,
        )
        try:
            # 延迟 import，保证模块本身可被单测在无网络环境导入
            import urllib.request
            import urllib.error

            req = urllib.request.Request(url, headers={"User-Agent": "local-injection-scanner/1.0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
            body = raw.decode("utf-8", errors="replace")
            record.status_code = status
            self.records.append(record)
            return HttpResponse(status, body)
        except urllib.error.HTTPError as exc:  # type: ignore[name-defined]
            # 4xx/5xx 仍然带有响应体，漏洞证据（数据库报错）常在 500 中
            try:
                raw = exc.read() if hasattr(exc, "read") else b""
            finally:
                exc.close()
            body = raw.decode("utf-8", errors="replace")
            record.status_code = exc.code
            self.records.append(record)
            return HttpResponse(exc.code, body, error=f"HTTP {exc.code}")
        except (urllib.error.URLError, socket.timeout, OSError) as exc:  # type: ignore[name-defined]
            record.error = str(exc)
            self.records.append(record)
            return HttpResponse(0, "", error=str(exc))
