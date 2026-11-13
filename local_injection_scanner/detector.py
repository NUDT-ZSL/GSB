"""响应分析与注入成功判定逻辑。

判定原则（对应验收第 10 条）：
  * 普通 200 响应、且响应中没有注入成功特征 -> 不报缺陷；
  * 响应中出现数据库错误等明确特征 -> 才报告缺陷；
  * 5xx 但不含任何注入特征 -> 记为该参数的请求错误，不算缺陷。

分类策略：以载荷大类（payload_category）为优先，再用响应特征确认。
这样含引号/分号的命令注入、NoSQL 载荷不会被 SQL 报错签名"抢先"归类。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_EVIDENCE_LEN = 500

# 数据库 / 解释器报错特征（小写匹配）
SQL_ERROR_SIGNATURES = [
    "sql syntax",
    "sqlite_error",
    "sqlite3.operationalerror",
    "unclosed quotation mark",
    "you have an error in your sql syntax",
    "ora-",
    "postgresql",
    "sqlstate",
    "odbc microsoft access driver",
    "warning: mysql",
    "syntax error at or near",
    "unterminated quoted string",
]

CMD_ERROR_SIGNATURES = [
    "uid=",
    "gid=",
    "root:",
    "is not recognized as an internal or external command",
    "/bin/sh:",
    "/bin/bash:",
]

SSTI_SIGNATURES = [
    "49",  # {{7*7}} / ${7*7} 被引擎求值
    "templateerror",
    "jinja2.exceptions",
    "twig error",
]

NOSQL_SIGNATURES = [
    "mongodb",
    "bson",
    "bad query",
    "unknown operator",
]

# XSS：必须是"可执行的脚本结构"被原样反射（标签 < 与属性都在），
# 单纯在文本里出现 onerror= 字样不算（修复版会 HTML 转义，尖括号变为 &lt;）
XSS_REFLECT_PATTERN = re.compile(
    r"<\s*(script|img|svg|iframe|body)\b[^>]*"
    r"(?:onerror|onload|onclick|onmouseover|alert)\s*=",
    re.IGNORECASE,
)
XSS_SCRIPT_TAG_PATTERN = re.compile(r"<\s*script\b", re.IGNORECASE)


@dataclass
class DetectionResult:
    """单个载荷的判定结果。"""

    is_vulnerable: bool
    injection_type: str | None
    evidence: str          # 截断后的响应片段
    status_code: int
    request_error: bool    # 网络错误 / 5xx 等请求级失败


def truncate_evidence(text: str, limit: int = MAX_EVIDENCE_LEN) -> str:
    """把响应片段截断到不超过 500 字符（验收第 8 条）。

    截断标记本身计入长度，保证返回值总长度严格 <= limit。
    """
    if text is None:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    suffix = f"...[截断,共{len(text)}字符]"
    keep = limit - len(suffix)
    return text[:max(keep, 0)] + suffix


def _has_any(text_lower: str, signatures: list[str]) -> bool:
    return any(sig in text_lower for sig in signatures)


def _confirm_by_category(category: str, body: str, lowered: str) -> bool:
    """按载荷大类确认响应是否呈现对应的注入成功特征。"""
    if category.startswith("sql_"):
        return _has_any(lowered, SQL_ERROR_SIGNATURES)
    if category == "cmd_injection":
        return _has_any(lowered, CMD_ERROR_SIGNATURES)
    if category == "ssti":
        return _has_any(lowered, SSTI_SIGNATURES)
    if category == "nosql":
        return _has_any(lowered, NOSQL_SIGNATURES)
    if category == "xss_reflected":
        return bool(XSS_SCRIPT_TAG_PATTERN.search(body) or XSS_REFLECT_PATTERN.search(body))
    return False


# 载荷大类 -> 缺陷类型名
CATEGORY_TYPE = {
    "sql_error": "SQL注入",
    "sql_boolean": "SQL注入",
    "sql_union": "SQL注入",
    "sql_stacked": "SQL注入",
    "cmd_injection": "命令注入",
    "ssti": "模板注入SSTI",
    "nosql": "NoSQL注入",
    "xss_reflected": "反射型XSS",
}


def classify_response(payload: str, body: str, status_code: int,
                      network_error: bool, payload_category: str | None = None,
                      fallback_type: str | None = None) -> DetectionResult:
    """根据响应内容区分正常响应与注入成功响应。

    优先依据 payload_category 做定向特征确认；category 缺失时退化为通用分类。
    仅当命中明确注入特征时 is_vulnerable 为 True。
    """
    evidence = truncate_evidence(body or "")
    request_error = network_error or status_code >= 500
    lowered = (body or "").lower()

    # 1) 大类优先：定向确认，避免跨类签名串扰
    if payload_category and payload_category in CATEGORY_TYPE:
        if _confirm_by_category(payload_category, body or "", lowered):
            return DetectionResult(
                True, CATEGORY_TYPE[payload_category], evidence, status_code, request_error
            )
        # 未命中本类特征：即使是 5xx 也只是请求错误，不构成缺陷
        return DetectionResult(False, None, evidence, status_code, request_error)

    # 2) 通用兜底分类（无大类信息时使用）
    if _has_any(lowered, SQL_ERROR_SIGNATURES):
        return DetectionResult(True, fallback_type or "SQL注入", evidence, status_code, request_error)
    if _has_any(lowered, CMD_ERROR_SIGNATURES):
        return DetectionResult(True, fallback_type or "命令注入", evidence, status_code, request_error)
    if _has_any(lowered, SSTI_SIGNATURES):
        return DetectionResult(True, fallback_type or "模板注入SSTI", evidence, status_code, request_error)
    if _has_any(lowered, NOSQL_SIGNATURES):
        return DetectionResult(True, fallback_type or "NoSQL注入", evidence, status_code, request_error)
    if XSS_SCRIPT_TAG_PATTERN.search(body or "") or XSS_REFLECT_PATTERN.search(body or ""):
        return DetectionResult(True, fallback_type or "反射型XSS", evidence, status_code, request_error)

    return DetectionResult(False, None, evidence, status_code, request_error)
