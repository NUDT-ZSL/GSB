"""确定性样本：探测用例清单与历史报告样本均由本模块生成。

不使用随机数，重复生成字节级一致，保证结论稳定可复核。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import (
    CTX_ATTR,
    CTX_SCRIPT,
    CTX_TEXT,
    ENTRY_FORM,
    ENTRY_PATH,
    ENTRY_QUERY,
    RESULT_RISK,
)

_MARKER_TEMPLATE = 'z{p}q{m}x<svc>"{idx}'


def make_marker(case_id: str, seq: int) -> str:
    """生成每条用例唯一、且含 HTML 特殊字符的探测标记。"""
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:8]
    return _MARKER_TEMPLATE.format(p=seq, m=digest, idx=seq)


def build_sample_cases() -> list[dict]:
    """覆盖三类入口与脚本/属性/文本三类上下文的标准用例集。"""
    raw = [
        # case_id, 入口类型, 方法, 路径, 参数
        ("TC-001", ENTRY_QUERY, "GET", "/reflect", "q"),
        ("TC-002", ENTRY_QUERY, "GET", "/attr", "q"),
        ("TC-003", ENTRY_QUERY, "GET", "/script", "q"),
        ("TC-004", ENTRY_PATH, "GET", "/path", "seg"),
        ("TC-005", ENTRY_FORM, "POST", "/form", "msg"),
        ("TC-006", ENTRY_QUERY, "GET", "/empty", "q"),
        ("TC-007", ENTRY_QUERY, "GET", "/safe", "q"),
        ("TC-008", ENTRY_FORM, "POST", "/form-unreachable", "msg"),
    ]
    cases = []
    for seq, (case_id, entry_type, method, path, param) in enumerate(raw, start=1):
        case = {
            "case_id": case_id,
            "entry_type": entry_type,
            "method": method,
            "path": path,
            "param": param,
            "marker": make_marker(case_id, seq),
        }
        # 用例 8：指向一个本机上未监听的端口，模拟入口不可达（仍是回环地址）
        if case_id == "TC-008":
            case["url"] = "http://127.0.0.1:9/unreachable-form"
        cases.append(case)
    return cases


def write_sample_cases(path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(build_sample_cases(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


# 历史报告样本：假设上次检测时 TC-001/002/003/004 存在风险，
# 且另有一个此后被删除的旧入口 TC-099 存在风险。
_HISTORY_CONTEXT = {
    "TC-001": CTX_TEXT,
    "TC-002": CTX_ATTR,
    "TC-003": CTX_SCRIPT,
    "TC-004": CTX_TEXT,
    "TC-099": CTX_ATTR,
}


def build_history_sample() -> dict:
    """构造确定性的历史报告样本（用于比对功能演示与测试）。"""
    cases = {c["case_id"]: c for c in build_sample_cases()}
    records = []
    for case_id in ("TC-001", "TC-002", "TC-003", "TC-004"):
        c = cases[case_id]
        records.append({
            "入口类型": c["entry_type"],
            "用例标识": c["case_id"],
            "标记": c["marker"],
            "结论": RESULT_RISK,
            "上下文类型": _HISTORY_CONTEXT[case_id],
            "全部上下文": [_HISTORY_CONTEXT[case_id]],
            "HTTP状态码": 200,
            "请求URL": "",
            "证据片段": "历史样本记录",
            "说明": "历史报告样本",
        })
    # 已被删除的旧入口：当前用例清单中不存在 -> 比对时体现为“已消除风险”
    records.append({
        "入口类型": ENTRY_QUERY,
        "用例标识": "TC-099",
        "标记": make_marker("TC-099", 99),
        "结论": RESULT_RISK,
        "上下文类型": CTX_ATTR,
        "全部上下文": [CTX_ATTR],
        "HTTP状态码": 200,
        "请求URL": "",
        "证据片段": "历史样本记录（旧入口）",
        "说明": "历史报告样本",
    })
    records.sort(key=lambda r: r["用例标识"])
    return {
        "报告版本": "1.0",
        "运行标识": "historysample0000",
        "目标基址": "http://127.0.0.1:8033",
        "修复状态标识": "历史基线-未修复",
        "汇总": {
            "总用例数": len(records),
            "存在风险数": len(records),
            "未获得有效响应数": 0,
            "无风险数": 0,
        },
        "用例记录": records,
        "结论取值约定": ["存在风险", "未获得有效响应", "无风险"],
    }


def write_history_sample(path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(build_history_sample(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out
