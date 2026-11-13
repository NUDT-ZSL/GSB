"""确定性的本地授权服务探测核心。

检测器只通过 HTTP 与目标交互，不导入目标服务的行为实现；对“响应是否泄漏
业务数据”的判定基于固定样本中的字段名、账目 ID 与余额字面量。
"""

from __future__ import annotations

import http.client
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from authsvc.constants import (
    ACCOUNT_FIELD,
    DEFAULT_SHARED_TOKEN,
    HEALTH_PATH,
    QUERY_PATH,
    TOKEN_FIELD,
    VALID_ACCOUNT_ID,
    WRONG_TOKEN_FOR_PROBE,
)
from authsvc.data import ACCOUNTS, BUSINESS_FIELDS

REPORT_SCHEMA_VERSION = 1

# 泄漏判定只看业务字段本身；``ok`` 是通用信封字段，不算业务数据。
_LEAK_KEYS = frozenset(BUSINESS_FIELDS)

# 已知业务值的哨兵：即使字段名被改写，出现这些字面量也视为泄漏。
_BUSINESS_SENTINELS = frozenset(
    value
    for account in ACCOUNTS
    for value in (
        account["account_id"],
        account["account_name"],
        account["balance"],
    )
)

_INTERNAL_DETAIL_MARKERS = (
    "traceback (most recent call last)",
    "traceback",
    'file "',
    ".py\", line",
    "zerodivisionerror",
    "keyerror",
    "jsondecodeerror",
)
_INTERNAL_DETAIL_KEYS = ("detail", "traceback", "stack", "exception", "debug")


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except Exception:
            return None


def _request(
    base_url: str,
    method: str,
    path: str,
    body: bytes | None = None,
    timeout: float = 5.0,
) -> Response:
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parts.scheme!r}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    connection_cls = (
        http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_cls(host, port, timeout=timeout)
    try:
        headers = {"Accept": "application/json", "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(body))
        connection.request(method, path, body=body, headers=headers)
        raw = connection.getresponse()
        data = raw.read()
        return Response(
            status=raw.status,
            headers={key.lower(): value for key, value in raw.getheaders()},
            body=data,
        )
    finally:
        connection.close()


def _post_json(base_url: str, payload: Any, timeout: float = 5.0) -> Response:
    return _request(
        base_url,
        "POST",
        QUERY_PATH,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        timeout,
    )


def _walk_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            keys.append(str(key))
            keys.extend(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.extend(_walk_keys(child))
    return keys


def business_data_exposed(response: Response) -> bool:
    """响应中是否出现账目业务字段或已知业务值。"""
    payload = response.json()
    if isinstance(payload, (dict, list)):
        if _LEAK_KEYS.intersection(_walk_keys(payload)):
            return True
    text = response.text
    return any(sentinel in text for sentinel in _BUSINESS_SENTINELS)


def internal_detail_exposed(response: Response) -> bool:
    """响应是否回显异常栈、源码路径或调试细节。"""
    text = response.text.lower()
    if any(marker in text for marker in _INTERNAL_DETAIL_MARKERS):
        return True
    payload = response.json()
    if isinstance(payload, dict):
        if any(key in payload for key in _INTERNAL_DETAIL_KEYS):
            return True
        if _looks_like_python_source(response.text):
            return True
    return False


_SOURCE_HINT = re.compile(r"(?:[A-Za-z]:\\|/)[^\s\"']+\.py", re.IGNORECASE)


def _looks_like_python_source(text: str) -> bool:
    return bool(_SOURCE_HINT.search(text))


def _conclusion_text(
    passed: bool,
    rejection_expected: bool,
    status: int,
    business: bool,
    detail: bool,
) -> str:
    if passed:
        if rejection_expected:
            return f"已拒绝（HTTP {status}），响应不含业务数据或内部细节"
        return f"合法请求通过（HTTP {status}），响应包含账目业务字段"
    if rejection_expected:
        if business:
            return f"未拒绝（HTTP {status}），响应泄漏账目业务数据"
        if detail:
            return f"未正确拒绝（HTTP {status}），响应泄漏内部异常细节"
        return f"未拒绝（HTTP {status}），响应不符合安全拒绝语义"
    return f"合法请求未通过（HTTP {status}），缺少预期的账目业务字段"


@dataclass
class Check:
    id: str
    category: str
    description: str
    expected: str
    status: str  # pass | vulnerability | fail
    outcome: str  # rejected | not_rejected | allowed | denied | unexpected
    http_status: int | None
    business_data_exposed: bool
    internal_detail_exposed: bool
    conclusion: str
    request_body: str | None = None
    response_body_preview: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "category": self.category,
            "description": self.description,
            "expected": self.expected,
            "status": self.status,
            "outcome": self.outcome,
            "is_vulnerability": self.status == "vulnerability",
            "http_status": self.http_status,
            "business_data_exposed": self.business_data_exposed,
            "internal_detail_exposed": self.internal_detail_exposed,
            "conclusion": self.conclusion,
        }
        if self.request_body is not None:
            data["request_body"] = self.request_body
        if self.response_body_preview is not None:
            data["response_body_preview"] = self.response_body_preview
        if self.extra:
            data["extra"] = self.extra
        return data


def _preview(text: str, limit: int = 400) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "...<truncated>"


def _check_unauthorized(
    base_url: str, check_id: str, description: str, payload: dict[str, Any]
) -> Check:
    response = _post_json(base_url, payload)
    business = business_data_exposed(response)
    detail = internal_detail_exposed(response)
    # 只有 4xx 且不含业务数据/内部细节才算“已拒绝”；其余（含 2xx、5xx）
    # 一律归为“未拒绝”，并按漏洞处理——未授权场景下不得计为通过。
    rejected = 400 <= response.status < 500 and not business and not detail
    status = "pass" if rejected else "vulnerability"
    outcome = "rejected" if rejected else "not_rejected"
    conclusion = _conclusion_text(rejected, True, response.status, business, detail)
    return Check(
        id=check_id,
        category="unauthorized",
        description=description,
        expected="请求被拒绝，响应不含业务数据或内部细节",
        status=status,
        outcome=outcome,
        http_status=response.status,
        business_data_exposed=business,
        internal_detail_exposed=detail,
        conclusion=conclusion,
        request_body=_preview(json.dumps(payload, ensure_ascii=False)),
        response_body_preview=_preview(response.text),
    )


def _check_legitimate(
    base_url: str, check_id: str, description: str, payload: dict[str, Any]
) -> tuple[Check, dict[str, Any] | None]:
    response = _post_json(base_url, payload)
    business = business_data_exposed(response)
    payload_json = response.json()
    required = set(_LEAK_KEYS)
    fields_ok = isinstance(payload_json, dict) and required.issubset(payload_json.keys())
    passed = response.status == 200 and fields_ok and business
    check = Check(
        id=check_id,
        category="legitimate",
        description=description,
        expected="HTTP 200 且响应包含账目业务字段",
        status="pass" if passed else "fail",
        outcome="allowed" if passed else "denied",
        http_status=response.status,
        business_data_exposed=business,
        internal_detail_exposed=internal_detail_exposed(response),
        conclusion=_conclusion_text(passed, False, response.status, business, False),
        request_body=_preview(json.dumps(payload, ensure_ascii=False)),
        response_body_preview=_preview(response.text),
    )
    return check, payload_json if isinstance(payload_json, dict) else None


def _check_malformed(
    base_url: str,
    check_id: str,
    description: str,
    raw_body: bytes,
    note: str,
) -> Check:
    """畸形请求检查：4xx 且不含业务数据/内部细节 = 已拒绝。"""
    response = _request(base_url, "POST", QUERY_PATH, raw_body)
    business = business_data_exposed(response)
    detail = internal_detail_exposed(response)
    # 4xx = 已拒绝；5xx 或 2xx = 未正确拒绝（旧版非法 JSON 即 500 + 异常栈）。
    rejected = 400 <= response.status < 500 and not business and not detail
    if rejected:
        status = "pass"
        outcome = "rejected"
    elif detail or business:
        status = "vulnerability"
        outcome = "not_rejected"
    else:
        status = "fail"
        outcome = "not_rejected"
    conclusion = _conclusion_text(rejected, True, response.status, business, detail)
    return Check(
        id=check_id,
        category="malformed_input",
        description=description,
        expected="请求被拒绝（4xx），响应不含业务数据或内部细节",
        status=status,
        outcome=outcome,
        http_status=response.status,
        business_data_exposed=business,
        internal_detail_exposed=detail,
        conclusion=conclusion,
        request_body=note,
        response_body_preview=_preview(response.text),
        extra={"declared_body_bytes": len(raw_body)},
    )


def _check_stability(
    base_url: str,
    baseline: dict[str, Any] | None,
    payload: dict[str, Any],
) -> Check:
    response = _post_json(base_url, payload)
    current = response.json()
    stable = (
        baseline is not None
        and isinstance(current, dict)
        and current == baseline
        and response.status == 200
    )
    return Check(
        id="consistency.legitimate_data_stable",
        category="consistency",
        description="探测器完成未授权与畸形探测后，合法账目数据保持一致",
        expected="探测前后同一合法查询返回完全相同的数据",
        status="pass" if stable else "fail",
        outcome="allowed" if stable else "unexpected",
        http_status=response.status,
        business_data_exposed=business_data_exposed(response),
        internal_detail_exposed=internal_detail_exposed(response),
        conclusion=(
            "探测前后合法账目数据一致，未被探测污染"
            if stable
            else "探测前后合法账目数据不一致或不可读取"
        ),
        request_body=_preview(json.dumps(payload, ensure_ascii=False)),
        response_body_preview=_preview(response.text),
    )


@dataclass
class Report:
    label: str
    base_url: str
    service_version: str | None
    checks: list[Check]
    health: dict[str, Any] | None

    @property
    def vulnerabilities(self) -> list[Check]:
        return [check for check in self.checks if check.status == "vulnerability"]

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if check.status == "fail"]

    @property
    def clean(self) -> bool:
        return not self.vulnerabilities and not self.failed

    def to_dict(self) -> dict[str, Any]:
        unauthorized = [c for c in self.checks if c.category == "unauthorized"]
        summary = {
            "total": len(self.checks),
            "passed": sum(1 for c in self.checks if c.status == "pass"),
            "vulnerabilities": len(self.vulnerabilities),
            "failed": len(self.failed),
            "unauthorized_rejected": sum(1 for c in unauthorized if c.outcome == "rejected"),
            "unauthorized_not_rejected": sum(
                1 for c in unauthorized if c.outcome == "not_rejected"
            ),
        }
        if self.clean:
            overall_status = "no_vulnerability"
            overall_text = "无漏洞：所有探测项均通过"
        else:
            overall_status = "vulnerability_found"
            problem_ids = [c.id for c in self.vulnerabilities + self.failed]
            overall_text = "发现漏洞/异常：" + ", ".join(problem_ids)
        return {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "tool": "authsvc-detector",
            "label": self.label,
            "target": {
                "base_url": self.base_url,
                "service_version": self.service_version,
                "health": self.health,
            },
            "checks": [check.to_dict() for check in self.checks],
            "summary": summary,
            "overall": {
                "status": overall_status,
                "conclusion": overall_text,
                "clean": self.clean,
            },
        }


def detect(
    base_url: str,
    token: str = DEFAULT_SHARED_TOKEN,
    label: str | None = None,
) -> Report:
    """对运行中的服务执行完整探测，返回确定性报告对象。"""
    base_url = base_url.rstrip("/")
    health_response = _request(base_url, "GET", HEALTH_PATH)
    health = health_response.json() if health_response.status == 200 else None
    service_version = health.get("version") if isinstance(health, dict) else None

    valid_payload = {TOKEN_FIELD: token, ACCOUNT_FIELD: VALID_ACCOUNT_ID}
    checks: list[Check] = []

    # 1) 先取合法基线，随后逐项探测，最后再次确认数据未被污染。
    legit_check, baseline = _check_legitimate(
        base_url,
        "legitimate.valid_token",
        "携带正确共享令牌查询受保护账目",
        valid_payload,
    )
    checks.append(legit_check)

    # 2) 未授权场景：缺失 / 空 / 错误令牌，逐项给出已拒绝或未拒绝。
    checks.append(
        _check_unauthorized(
            base_url,
            "unauthorized.missing_token",
            "缺失令牌查询受保护账目",
            {ACCOUNT_FIELD: VALID_ACCOUNT_ID},
        )
    )
    checks.append(
        _check_unauthorized(
            base_url,
            "unauthorized.empty_token",
            "空字符串令牌查询受保护账目",
            {TOKEN_FIELD: "", ACCOUNT_FIELD: VALID_ACCOUNT_ID},
        )
    )
    checks.append(
        _check_unauthorized(
            base_url,
            "unauthorized.wrong_token",
            "错误令牌查询受保护账目",
            {TOKEN_FIELD: WRONG_TOKEN_FOR_PROBE, ACCOUNT_FIELD: VALID_ACCOUNT_ID},
        )
    )

    # 3) 畸形输入：非法 JSON 与超长字段。
    malformed_body = (
        b'{"token":"' + token.encode("utf-8") + b'","account_id":"ACC-1001"'
    )
    checks.append(
        _check_malformed(
            base_url,
            "malformed.invalid_json",
            "携带正确令牌但请求体为非法 JSON",
            malformed_body,
            note='{"token":"<token>","account_id":"ACC-1001"  (truncated JSON)',
        )
    )
    oversized_payload = {
        TOKEN_FIELD: token,
        ACCOUNT_FIELD: "A" * 8192,
    }
    checks.append(
        _check_malformed(
            base_url,
            "malformed.oversized_field",
            "携带正确令牌但 account_id 超长（8192 字符）",
            json.dumps(oversized_payload, separators=(",", ":")).encode("utf-8"),
            note="account_id = 'A' * 8192",
        )
    )

    # 4) 稳定性：确认上述探测未污染合法数据。
    checks.append(_check_stability(base_url, baseline, valid_payload))

    return Report(
        label=label or service_version or "unknown",
        base_url=base_url,
        service_version=service_version,
        checks=checks,
        health=health if isinstance(health, dict) else None,
    )
