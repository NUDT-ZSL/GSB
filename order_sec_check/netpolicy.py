"""网络访问策略：只允许访问配置中的本地回环地址。

任何外部主机（含公网 IP、局域网 IP、非回环域名）一律整体拒绝，
拒绝发生在 socket 建立之前，保证不发出任何请求。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = ("http", "https")
_LOCAL_HOSTNAMES = {"localhost", "ip6-localhost"}


class PolicyError(Exception):
    """目标地址不满足本地访问策略。"""


def _is_loopback_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return bool(addr.is_loopback)


def assert_local_url(url: str) -> None:
    """校验 URL 指向本地回环地址，不满足则抛出 PolicyError。"""
    if not isinstance(url, str) or not url.strip():
        raise PolicyError("目标地址为空，拒绝执行")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise PolicyError(
            f"目标地址 {url!r} 的协议 {parts.scheme!r} 不受支持，仅允许 http/https"
        )
    host = parts.hostname
    if not host:
        raise PolicyError(f"目标地址 {url!r} 缺少主机名，拒绝执行")
    host_lower = host.lower().strip("[]")
    if host_lower in _LOCAL_HOSTNAMES:
        return
    if _is_loopback_ip(host_lower):
        return
    # 域名形式：先解析再判断，解析失败也拒绝（默认拒绝原则）
    try:
        infos = socket.getaddrinfo(host_lower, parts.port or 80, proto=socket.IPPROTO_TCP)
    except OSError:
        raise PolicyError(
            f"目标主机 {host!r} 无法解析且不是回环地址，拒绝发起任何请求"
        ) from None
    for info in infos:
        ip = info[4][0]
        if not _is_loopback_ip(ip):
            raise PolicyError(
                f"目标主机 {host!r} 解析到非回环地址 {ip}，"
                "本工具仅允许访问本地地址，整体拒绝且不发起任何请求"
            )
