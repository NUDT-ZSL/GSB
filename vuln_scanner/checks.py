"""漏洞数据模型与三类检测逻辑（SQL 注入 / XSS / 目录遍历）。

检测基于样本服务中的确定性标记与状态码，离线、可重复、无误报。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from urllib.parse import urlencode, quote

from .config import (
    CHECK_SQLI, CHECK_XSS, CHECK_TRAVERSAL,
    SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW,
)
from .http_client import HttpResponse

# 检测项 -> 严重性 / 中文名 / 修复建议
CHECK_METADATA = {
    CHECK_SQLI: {
        "severity": SEVERITY_HIGH,
        "label": "SQL 注入",
        "remediation": "使用参数化查询/预编译语句，禁止将用户输入拼接进 SQL；"
                       "并对数据库账号做最小权限配置。",
    },
    CHECK_XSS: {
        "severity": SEVERITY_MEDIUM,
        "label": "跨站脚本",
        "remediation": "对所有回显到 HTML 的用户输入做上下文相关转义"
                       "（如 html.escape），并设置 Content-Security-Policy。",
    },
    CHECK_TRAVERSAL: {
        "severity": SEVERITY_LOW,
        "label": "目录遍历",
        "remediation": "对文件路径做规范化并限制在白名单根目录内，拒绝包含 "
                       "..、绝对路径或反斜杠的输入，使用间接 ID 映射文件。",
    },
}

# 与 sample_app.py 中的标记一一对应
SQLI_MARKER = "sql-injection-confirmed"
XSS_MARKER = "<xss-echo>"
TRAVERSAL_MARKER = "traversal-secret-confirmed"


@dataclass(frozen=True)
class Finding:
    type: str
    type_label: str
    url: str
    parameter: str | None
    severity: str
    remediation: str

    def key(self) -> tuple:
        """去重键：同目标同类型同参数只保留一条（需求 15）。"""
        return self.type, self.url, self.parameter

    def to_dict(self) -> dict:
        return asdict(self)


# 各检测项的探测载荷（对样本服务确定生效）
_PAYLOADS = {
    CHECK_SQLI: ["' OR '1'='1", "admin' OR 1=1 -- "],
    CHECK_XSS: ["<script>alert(1)</script>", "\"><img src=x onerror=alert(1)>"],
    CHECK_TRAVERSAL: ["../etc/passwd", "..\\etc\\passwd", "/etc/passwd"],
}


def build_probe_url(base_url: str, path: str, check: str,
                    parameter: str | None, placeholder: str | None) -> str:
    """根据注入点类型构造探测 URL。"""
    base = base_url.rstrip("/")
    payload = _PAYLOADS[check][0]

    if parameter is None:  # 路径型注入点：替换占位符，无参数名
        if not placeholder:
            raise ValueError("路径型注入点必须提供 placeholder")
        encoded = quote(payload, safe="")
        return base + path.replace(placeholder, encoded)

    query = urlencode({parameter: payload})
    return base + path + "?" + query


def analyze(check: str, url: str, parameter: str | None,
            resp: HttpResponse) -> Finding | None:
    """根据响应判断漏洞是否存在；未命中返回 None。"""
    meta = CHECK_METADATA[check]
    hit = False
    if check == CHECK_SQLI:
        hit = resp.status == 200 and SQLI_MARKER in resp.body
    elif check == CHECK_XSS:
        # 样本服务把载荷原样回显在两个标记之间
        hit = resp.status == 200 and XSS_MARKER in resp.body and (
            "<script>" in resp.body or "onerror=" in resp.body)
    elif check == CHECK_TRAVERSAL:
        hit = resp.status == 200 and TRAVERSAL_MARKER in resp.body

    if not hit:
        return None
    return Finding(
        type=check,
        type_label=meta["label"],
        url=url,
        parameter=parameter,
        severity=meta["severity"],
        remediation=meta["remediation"],
    )
