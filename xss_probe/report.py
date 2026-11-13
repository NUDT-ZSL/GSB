"""用例清单加载、JSON 报告生成与历史报告比对。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import (
    ENTRY_FORM,
    ENTRY_PATH,
    ENTRY_QUERY,
    RESULT_NO_RESPONSE,
    RESULT_RISK,
    VALID_ENTRY_TYPES,
    VALID_RESULTS,
    TestCase,
    result_sort_key,
)

_ALLOWED_METHODS = ("GET", "POST")


def load_cases(path: str | Path) -> list[TestCase]:
    """从 JSON 文件加载探测用例并做字段校验。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("用例清单必须是非空 JSON 数组")

    cases: list[TestCase] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 条用例不是 JSON 对象")
        case_id = str(item.get("case_id", "")).strip()
        if not case_id:
            raise ValueError(f"第 {index + 1} 条用例缺少 case_id")
        if case_id in seen_ids:
            raise ValueError(f"用例标识重复: {case_id}")
        seen_ids.add(case_id)

        entry_type = item.get("entry_type")
        if entry_type not in VALID_ENTRY_TYPES:
            raise ValueError(
                f"用例 {case_id} 的 entry_type 非法: {entry_type!r}，"
                f"允许值: {', '.join(VALID_ENTRY_TYPES)}"
            )
        method = str(item.get("method", "GET")).upper()
        if method not in _ALLOWED_METHODS:
            raise ValueError(f"用例 {case_id} 的 method 非法: {method!r}")
        if entry_type == ENTRY_FORM and method != "POST":
            raise ValueError(f"表单回显用例 {case_id} 必须使用 POST")
        if entry_type in (ENTRY_QUERY, ENTRY_PATH) and method != "GET":
            raise ValueError(f"{entry_type}用例 {case_id} 必须使用 GET")

        path_value = str(item.get("path", ""))
        if not path_value.startswith("/"):
            raise ValueError(f"用例 {case_id} 的 path 必须以 / 开头")
        param = str(item.get("param", ""))
        marker = str(item.get("marker", ""))
        if not marker:
            raise ValueError(f"用例 {case_id} 缺少唯一标记 marker")
        url_override = item.get("url")
        if url_override is not None and not isinstance(url_override, str):
            raise ValueError(f"用例 {case_id} 的 url 必须是字符串")

        cases.append(
            TestCase(
                case_id=case_id,
                entry_type=entry_type,
                method=method,
                path=path_value,
                param=param,
                marker=marker,
                url=url_override,
            )
        )
    return cases


def _risk_key(record: dict) -> list:
    """新增/消除风险的匹配组合键：入口类型 + 用例标识 + 标记。"""
    return [record["入口类型"], record["用例标识"], record["标记"]]


def build_run_id(base_url: str, records: list[dict]) -> str:
    """基于目标与全部结论的确定性运行标识（不含时间，保证可复核）。"""
    digest = hashlib.sha256()
    digest.update(base_url.encode("utf-8"))
    for record in records:
        digest.update("|".join(
            [record["入口类型"], record["用例标识"], record["标记"], record["结论"]]
        ).encode("utf-8"))
    return digest.hexdigest()[:16]


def build_report(base_url: str, fix_status: str, records: list[dict]) -> dict:
    """组装完整报告对象。"""
    records = sorted(
        records, key=lambda r: (result_sort_key(r["结论"]), r["用例标识"])
    )
    total = len(records)
    risk_count = sum(1 for r in records if r["结论"] == RESULT_RISK)
    no_resp_count = sum(1 for r in records if r["结论"] == RESULT_NO_RESPONSE)
    safe_count = total - risk_count - no_resp_count
    return {
        "报告版本": "1.0",
        "运行标识": build_run_id(base_url, records),
        "目标基址": base_url,
        "修复状态标识": fix_status,
        "汇总": {
            "总用例数": total,
            "存在风险数": risk_count,
            "未获得有效响应数": no_resp_count,
            "无风险数": safe_count,
        },
        "用例记录": records,
        "结论取值约定": list(VALID_RESULTS),
    }


def save_report(report: dict, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    return out


def load_report(path: str | Path) -> dict:
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    if "用例记录" not in report:
        raise ValueError(f"历史报告格式不正确，缺少 用例记录 字段: {path}")
    return report


def compare_reports(history: dict, current: dict) -> dict:
    """按组合键比对两份报告中的风险项。"""
    old_risks = {
        tuple(_risk_key(r)): r for r in history["用例记录"]
        if r["结论"] == RESULT_RISK
    }
    new_risks = {
        tuple(_risk_key(r)): r for r in current["用例记录"]
        if r["结论"] == RESULT_RISK
    }
    introduced = [new_risks[k] for k in sorted(new_risks.keys() - old_risks.keys())]
    resolved = [old_risks[k] for k in sorted(old_risks.keys() - new_risks.keys())]
    return {
        "历史报告运行标识": history.get("运行标识", ""),
        "本次运行标识": current.get("运行标识", ""),
        "新增风险": [
            {
                "入口类型": r["入口类型"],
                "用例标识": r["用例标识"],
                "标记": r["标记"],
                "上下文类型": r["上下文类型"],
            }
            for r in introduced
        ],
        "已消除风险": [
            {
                "入口类型": r["入口类型"],
                "用例标识": r["用例标识"],
                "标记": r["标记"],
                "上下文类型": r["上下文类型"],
            }
            for r in resolved
        ],
        "新增风险数": len(introduced),
        "已消除风险数": len(resolved),
    }
