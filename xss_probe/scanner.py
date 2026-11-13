"""探测执行与风险判定核心。

判定原则（不依赖状态码、不依赖“标记是否出现”这一单一事实）：

* 请求失败（连接拒绝、超时、DNS 失败等）或响应体为空 -> 未获得有效响应；
* 标记以“未转义原样”出现在响应体 -> 存在风险，并标注回显上下文；
* 标记仅以 HTML 转义形式出现 -> 无风险（转义后回显）；
* 标记完全未出现 -> 无风险（无回显），上下文记为“无回显”。
"""

from __future__ import annotations

import socket
from urllib.error import HTTPError, URLError

from .models import (
    CTX_NA,
    CTX_NONE,
    RESULT_NO_RESPONSE,
    RESULT_RISK,
    RESULT_SAFE,
    TestCase,
    result_sort_key,
)
from .netutil import assert_loopback_url, build_target_url, send_request
from .parserutil import classify_contexts, escaped_forms

# 证据片段长度，便于人工复核，不影响判定
_EVIDENCE_LIMIT = 120


def _evidence(document: str, marker: str) -> str:
    index = document.find(marker)
    if index < 0:
        return ""
    start = max(0, index - 30)
    end = min(len(document), index + len(marker) + 30)
    snippet = document[start:end].replace("\n", " ").replace("\r", " ")
    return snippet[:_EVIDENCE_LIMIT]


def scan_case(base_url: str, case: TestCase, timeout: float = 5.0) -> dict:
    """执行单条用例并产出一条报告记录。单条失败不影响其他用例。"""
    target_url = build_target_url(base_url, case)

    # 每条用例的实际目标（含 url 覆盖）都必须再次做回环校验
    try:
        assert_loopback_url(target_url)
    except Exception as exc:  # noqa: BLE001 - 统一归入无效响应并附原因
        return _no_response_record(case, target_url, f"回环校验失败: {exc}")

    try:
        status_code, body_bytes = send_request(
            target_url, case.method, case.param, case.marker, timeout=timeout
        )
    except HTTPError as exc:
        # HTTP 错误状态：若带响应体仍按体内容分析；空体才记无效响应
        body_bytes = exc.read() if exc.fp is not None else b""
        status_code = exc.code
        if not body_bytes:
            return _no_response_record(
                case, target_url, f"HTTP {status_code} 且响应体为空"
            )
    except (URLError, socket.timeout, OSError) as exc:
        return _no_response_record(case, target_url, f"请求失败: {exc}")

    if not body_bytes:
        return _no_response_record(case, target_url, "响应体缺失或为空")

    document = body_bytes.decode("utf-8", errors="replace")

    # 1) 未转义原样出现 -> 存在风险（判定基于转义状态，而非状态码/单纯出现）
    if case.marker in document:
        contexts = classify_contexts(document, case.marker)
        return {
            "入口类型": case.entry_type,
            "用例标识": case.case_id,
            "标记": case.marker,
            "结论": RESULT_RISK,
            "上下文类型": contexts[0] if contexts else CTX_NONE,
            "全部上下文": contexts,
            "HTTP状态码": status_code,
            "请求URL": target_url,
            "证据片段": _evidence(document, case.marker),
            "说明": "标记以未转义形式进入 HTML",
        }

    # 2) 仅以转义形式出现 -> 无风险；仍标注其实际回显上下文，
    #    与“完全无回显”区分开。
    for form in escaped_forms(case.marker):
        if form and form in document:
            safe_contexts = classify_contexts(document, form)
            return {
                "入口类型": case.entry_type,
                "用例标识": case.case_id,
                "标记": case.marker,
                "结论": RESULT_SAFE,
                "上下文类型": safe_contexts[0] if safe_contexts else CTX_NONE,
                "全部上下文": safe_contexts,
                "HTTP状态码": status_code,
                "请求URL": target_url,
                "证据片段": "",
                "说明": "标记经 HTML 转义后回显，未发现可执行注入",
            }

    # 3) 完全未回显 -> 无风险（无回显）
    return {
        "入口类型": case.entry_type,
        "用例标识": case.case_id,
        "标记": case.marker,
        "结论": RESULT_SAFE,
        "上下文类型": CTX_NONE,
        "全部上下文": [],
        "HTTP状态码": status_code,
        "请求URL": target_url,
        "证据片段": "",
        "说明": "响应体中未发现标记回显",
    }


def _no_response_record(case: TestCase, target_url: str, reason: str) -> dict:
    return {
        "入口类型": case.entry_type,
        "用例标识": case.case_id,
        "标记": case.marker,
        "结论": RESULT_NO_RESPONSE,
        "上下文类型": CTX_NA,
        "全部上下文": [],
        "HTTP状态码": None,
        "请求URL": target_url,
        "证据片段": "",
        "说明": reason,
    }


def run_scan(base_url: str, cases: list[TestCase], timeout: float = 5.0) -> list[dict]:
    """对全部用例逐条探测；任何单条异常都被隔离在该条记录内。"""
    records: list[dict] = []
    for case in cases:
        records.append(scan_case(base_url, case, timeout=timeout))
    records.sort(key=lambda r: (result_sort_key(r["结论"]), r["用例标识"]))
    return records
