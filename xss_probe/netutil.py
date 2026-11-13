"""回环地址校验与 HTTP 请求发送（仅标准库 urllib）。"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse, urlencode, quote

from .models import ENTRY_PATH

_ALLOWED_SCHEMES = ("http", "https")


class NotLoopbackError(ValueError):
    """目标基址不是本机回环地址。"""


def assert_loopback_url(url: str) -> None:
    """校验 URL 指向本机回环地址，否则抛出 NotLoopbackError。

    判定方式：解析主机名得到全部 IP，逐个确认属于回环网段
    （IPv4 127.0.0.0/8，IPv6 ::1）。主机名为 localhost 时同样解析校验，
    避免只相信字符串白名单。
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise NotLoopbackError(
            f"仅支持 http/https 协议的本机地址，收到协议: {parsed.scheme!r}（{url}）"
        )
    host = parsed.hostname
    if not host:
        raise NotLoopbackError(f"无法解析目标主机名: {url!r}，仅支持本机回环地址")

    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise NotLoopbackError(
            f"主机名 {host!r} 无法解析，无法确认其为本机回环地址: {exc}"
        ) from exc

    # 排序而非 set 迭代：set 的迭代顺序受哈希随机化影响，
    # 会破坏“同一目标重复运行完全一致”的可复核性。
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise NotLoopbackError(f"主机名 {host!r} 未解析出任何地址，拒绝执行: {url}")

    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        if not ip.is_loopback:
            raise NotLoopbackError(
                f"目标 {host!r} 解析到非回环地址 {addr}，本工具仅允许对本机回环地址"
                "（127.0.0.0/8、::1）执行，拒绝访问外部目标。"
            )


def build_target_url(base_url: str, case) -> str:
    """根据用例构造实际请求 URL。"""
    if case.url:
        return case.url
    base = base_url.rstrip("/")
    if case.entry_type == ENTRY_PATH:
        # 路径片段：标记按路径段做百分号编码后拼入
        return f"{base}{case.path.rstrip('/')}/{quote(case.marker, safe='')}"
    if case.method.upper() == "GET":
        separator = "&" if "?" in case.path else "?"
        return f"{base}{case.path}{separator}{urlencode({case.param: case.marker})}"
    # POST 表单：参数在请求体中，URL 不含查询串
    return f"{base}{case.path}"


def send_request(target_url: str, method: str, param: str, marker: str,
                 timeout: float = 5.0) -> tuple[int, bytes]:
    """发送请求，返回 (HTTP 状态码, 响应体字节)。

    网络层错误以异常形式向上抛出，由调用方归入“未获得有效响应”。
    延迟 import urllib，保持模块职责清晰。
    """
    from urllib.request import Request, urlopen

    data = None
    headers = {"User-Agent": "xss-probe/1.0 (loopback-only authorized test)"}
    if method.upper() == "POST":
        data = urlencode({param: marker}).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = Request(target_url, data=data, headers=headers, method=method.upper())
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 已做回环校验
        return resp.getcode(), resp.read()
