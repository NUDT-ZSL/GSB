"""检测引擎：构造请求、发送到本地接口、按规则判定是否命中。

设计要点：
- 只使用标准库 urllib，不跟随跨主机重定向（重定向目标也走本地策略校验）；
- 连接失败/超时统一归类为"暂时不可达"（incomplete），保留后续再次检测的机会，
  不计入未命中；
- 凭据在写入摘要前脱敏；
- 判定完全由用例中的声明式规则驱动，避免凭响应特征字符串拍脑袋。
"""

from __future__ import annotations

import json as json_lib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import redact

_MAX_BODY_CHARS = 4000
_SUMMARY_CHARS = 600


class TargetUnreachable(Exception):
    """目标暂时不可达（连接被拒/超时/DNS 失败等），属于可重试状态。"""


@dataclass
class HttpResult:
    status_code: int
    headers: dict[str, str]
    body_text: str
    json_body: Any
    request_summary: dict[str, Any]
    response_summary: dict[str, Any]


@dataclass
class RuleVerdict:
    hit: bool
    detail: str
    matched_rule: dict[str, Any] | None = None
    rule_results: list[dict[str, Any]] = field(default_factory=list)


def _build_url(base_url: str, path: str, params: dict[str, Any]) -> str:
    base = base_url.rstrip("/")
    query = urllib.parse.urlencode(params, doseq=True)
    return f"{base}{path}?{query}" if query else f"{base}{path}"


def _safe_redirect_handler():
    # 默认的 HTTPRedirectHandler 会自动跟随跳转；这里每一跳都做本地策略校验。
    from .netpolicy import PolicyError, assert_local_url

    class _LocalOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            try:
                assert_local_url(newurl)
            except PolicyError as exc:
                raise TargetUnreachable(f"重定向目标不满足本地策略：{exc}") from exc
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return _LocalOnlyRedirectHandler()


def send_request(
    base_url: str,
    request_spec: dict[str, Any],
    secrets: list[str],
    timeout: float = 3.0,
) -> HttpResult:
    method = request_spec["method"].upper()
    url = _build_url(base_url, request_spec["path"], request_spec.get("params", {}))
    headers = dict(request_spec.get("headers", {}))
    data = None
    if method == "POST" and request_spec.get("body") is not None:
        data = json_lib.dumps(request_spec["body"]).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_safe_redirect_handler())
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.getcode()
            resp_headers = {k: v for k, v in resp.headers.items()}
            raw = resp.read(_MAX_BODY_CHARS + 1)
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 仍然是有效响应，安全检测需要根据其内容判定
        status = exc.code
        resp_headers = {k: v for k, v in (exc.headers or {}).items()}
        raw = exc.read(_MAX_BODY_CHARS + 1) if exc.fp else b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TargetUnreachable(f"连接失败/超时：{exc}") from exc

    truncated = len(raw) > _MAX_BODY_CHARS
    body_bytes = raw[:_MAX_BODY_CHARS]
    body_text = body_bytes.decode("utf-8", errors="replace")
    try:
        json_body = json_lib.loads(body_text)
    except (json_lib.JSONDecodeError, ValueError):
        json_body = None

    request_summary = {
        "method": method,
        "url": redact.redact_text(url, secrets),
        "headers": redact.redact_headers(headers),
    }
    if data is not None:
        request_summary["body"] = redact.redact_text(
            data.decode("utf-8"), secrets
        )
    response_summary = {
        "status_code": status,
        "body": _short(body_text),
        "truncated": truncated,
    }
    return HttpResult(
        status_code=status,
        headers=resp_headers,
        body_text=body_text,
        json_body=json_body,
        request_summary=request_summary,
        response_summary=response_summary,
    )


def _short(text: str) -> str | None:
    if not text:
        return None
    return text if len(text) <= _SUMMARY_CHARS else text[:_SUMMARY_CHARS] + "...(截断)"


def _json_path(data: Any, path: str) -> tuple[bool, Any]:
    """极简 JSON Pointer 取值：a.b.0.c；返回 (是否找到, 值)。"""
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
        elif isinstance(current, list):
            try:
                idx = int(part)
            except ValueError:
                return False, None
            if idx < 0 or idx >= len(current):
                return False, None
            current = current[idx]
        else:
            return False, None
    return True, current


def evaluate_rules(result: HttpResult, rules: list[dict[str, Any]]) -> RuleVerdict:
    rule_results: list[dict[str, Any]] = []
    matched: dict[str, Any] | None = None
    for rule in rules:
        rtype = rule["type"]
        expected = rule["expected"]
        ok = False
        actual: Any = None
        detail = ""
        if rtype == "status_code_equals":
            actual = result.status_code
            ok = actual == expected
            detail = f"状态码 {actual} == {expected}"
        elif rtype == "body_contains":
            actual = _short(result.body_text)
            ok = str(expected) in result.body_text
            detail = f"响应体包含 {expected!r}: {ok}"
        elif rtype == "body_not_contains":
            actual = _short(result.body_text)
            ok = str(expected) not in result.body_text
            detail = f"响应体不包含 {expected!r}: {ok}"
        elif rtype in ("json_path_equals", "json_len_gte", "json_len_equals"):
            found, value = _json_path(result.json_body, rule["path"])
            if not found:
                detail = f"JSON 路径 {rule['path']!r} 不存在"
                actual = None
            elif rtype == "json_path_equals":
                actual = value
                ok = value == expected
                detail = f"JSON 路径 {rule['path']!r} 实际值 {value!r}，期望 {expected!r}"
            elif rtype == "json_len_gte":
                actual = len(value) if isinstance(value, (list, str, dict)) else None
                ok = actual is not None and actual >= expected
                detail = f"JSON 路径 {rule['path']!r} 长度 {actual} >= {expected}"
            else:  # json_len_equals
                actual = len(value) if isinstance(value, (list, str, dict)) else None
                ok = actual is not None and actual == expected
                detail = f"JSON 路径 {rule['path']!r} 长度 {actual} == {expected}"
        rule_results.append(
            {
                "rule": rule,
                "satisfied": ok,
                "actual": actual,
                "detail": detail,
            }
        )
        if ok and matched is None:
            matched = rule

    hit = matched is not None
    return RuleVerdict(
        hit=hit,
        detail="命中" if hit else "所有判定规则均未满足",
        matched_rule=matched,
        rule_results=rule_results,
    )
