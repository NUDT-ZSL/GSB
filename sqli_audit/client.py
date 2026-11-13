"""受限的本地 HTTP 客户端（标准库 urllib 实现）。

约束：
- 只访问已通过配置校验的回环地址；
- 不跟随重定向，避免被诱导离开本机；
- 不携带任何凭据/Cookie；
- 所有网络异常都被捕获并结构化返回，由扫描器决定是否标记“未完成”，
  单个探测失败不会中断整体流程。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .config import TargetConfig


@dataclass(frozen=True)
class ProbeResponse:
    """一次探测的确定性结果快照。"""

    reached: bool                 # 是否拿到任何 HTTP 响应
    status: int | None            # HTTP 状态码（未连通时为 None）
    body_text: str                # 响应体文本（截断保护，见 _MAX_BODY）
    json_ok: bool                 # 响应体是否为合法 JSON
    transport_error: str | None   # 传输层错误摘要（未连通时有值）

    @property
    def failed(self) -> bool:
        return not self.reached


_MAX_BODY = 1_000_000


def _build_url(cfg: TargetConfig, query_fields: dict[str, str]) -> str:
    url = cfg.url
    if query_fields:
        encoded = urllib_parse.urlencode(sorted(query_fields.items()))
        url = f"{url}?{encoded}"
    return url


def send(cfg: TargetConfig, fields: dict[str, str]) -> ProbeResponse:
    """按目标配置发送一次请求，fields 为本次请求使用的全部参数值。

    参数按 location 决定进入 query string 还是表单 body；
    未在 fields 中给出的参数不发送（每个参数独立探测，其余参数取基线值）。
    """
    query_fields: dict[str, str] = {}
    body_fields: dict[str, str] = {}
    for p in cfg.params:
        if p.name not in fields:
            continue
        if p.location == "query":
            query_fields[p.name] = fields[p.name]
        else:
            body_fields[p.name] = fields[p.name]

    url = _build_url(cfg, query_fields)
    data = None
    headers = {"Accept": "application/json", "User-Agent": "sqli-audit-local/1.0"}
    if cfg.method == "POST":
        data = urllib_parse.urlencode(sorted(body_fields.items())).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib_request.Request(url, data=data, headers=headers, method=cfg.method)
    opener = urllib_request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=cfg.timeout_seconds) as resp:
            status = resp.getcode()
            raw = resp.read(_MAX_BODY)
    except urllib_error.HTTPError as exc:
        # 服务端返回了真实 HTTP 响应（含 4xx/5xx），视为“已连通”
        status = exc.code
        try:
            raw = exc.read(_MAX_BODY)
        except Exception:
            raw = b""
        finally:
            exc.close()
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        return ProbeResponse(
            reached=False,
            status=None,
            body_text="",
            json_ok=False,
            transport_error=type(exc).__name__ + ": " + str(exc)[:200],
        )

    text = raw.decode("utf-8", errors="replace")
    try:
        json.loads(text)
        json_ok = True
    except ValueError:
        json_ok = False
    return ProbeResponse(
        reached=True,
        status=status,
        body_text=text,
        json_ok=json_ok,
        transport_error=None,
    )


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    """禁止重定向：本地自查不允许请求被引导到其他主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib_error.HTTPError(
            req.full_url,
            code,
            f"重定向已被策略拒绝: {newurl}",
            headers,
            fp,
        )
