"""标准库 HTTP 客户端：统一封装超时、连接拒绝与非 2xx 状态。"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass


class TargetUnreachable(Exception):
    """连接被拒绝、DNS 失败等连接层错误（目标整体不可用）。"""


class RequestTimeout(Exception):
    """等待上限内无响应。"""


@dataclass
class HttpResponse:
    url: str
    status: int
    body: str
    content_type: str


def http_get(url: str, timeout: float) -> HttpResponse:
    request = urllib.request.Request(url, method="GET", headers={
        "User-Agent": "local-authorized-vuln-scanner/1.0",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:  # 4xx/5xx 同样返回响应体供检测与告警
        raw = exc.read() if hasattr(exc, "read") else b""
        return HttpResponse(url, exc.code,
                            raw.decode("utf-8", errors="replace"),
                            exc.headers.get("Content-Type", "") if exc.headers else "")
    except (urllib.error.URLError, socket.gaierror, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout) or isinstance(exc, socket.timeout):
            raise RequestTimeout(url)
        # ConnectionRefusedError、NameResolutionError 等统一归入不可达
        raise TargetUnreachable("%s -> %s" % (url, reason))
    except socket.timeout:
        raise RequestTimeout(url)

    return HttpResponse(url, 200, raw.decode("utf-8", errors="replace"),
                        content_type)
