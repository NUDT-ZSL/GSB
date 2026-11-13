#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地弱口令基线自查工具（纯本地、确定性，不联网、不读写真实凭据）。

输入：一个 JSON 文件，内容为数组，每个元素含 name、role（admin/user）、password。
输出：JSON 分类报告，按风险排序，口令脱敏；损坏条目只记警告并跳过，不影响整体检测。

风险规则：
  - 高风险：命中内置常见弱口令清单（<=20 个模拟值）。
  - 中风险：纯数字、纯字母或长度不足 6 位；password 为 null/缺失（缺失口令）归入中风险。
  - 低风险：不满足上述任一条件的合规口令。
  - admin 账户命中高/中风险时，额外标注管理员账户风险。
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# 内置常见弱口令清单（模拟值，共 20 个；刻意不含 12345678 / abcdef，
# 以便它们走“纯数字/纯字母”的中风险规则）
WEAK_PASSWORDS = frozenset(
    [
        "123456",
        "password",
        "123456789",
        "12345",
        "qwerty",
        "1234567",
        "admin",
        "letmein",
        "abc123",
        "password1",
        "admin123",
        "welcome",
        "monkey",
        "dragon",
        "master",
        "login",
        "000000",
        "passw0rd",
        "iloveyou",
        "111111",
    ]
)

VALID_ROLES = ("admin", "user")


def _force_utf8_console() -> None:
    """强制标准流使用 UTF-8，避免中文 Windows 代码页（如 GBK）导致输出乱码。

    隔离模式（python -I）会忽略 PYTHONIOENCODING 等环境变量，因此在程序内
    显式重配置，保证任何调用方式下控制台输出都是确定的 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


_force_utf8_console()

# 风险排序权重：高风险在前，缺失口令归入中风险
RISK_ORDER = {"high": 0, "medium": 1, "low": 2}


def mask_password(pwd: Optional[str]) -> str:
    """口令脱敏：只显示首尾字符，中间以等长星号替换；缺失口令显示 ***。"""
    if pwd is None:
        return "***"
    if not isinstance(pwd, str):
        return "***"
    if len(pwd) <= 2:
        # 长度不足 3 时没有可保留的“中间段”，整体等长打码，避免泄露
        return "*" * len(pwd)
    return pwd[0] + "*" * (len(pwd) - 2) + pwd[-1]


def classify_password(pwd: Optional[str]) -> Tuple[str, str, List[str]]:
    """
    判定单条口令的风险。
    返回 (risk_level, risk_category, reasons)。
    """
    if pwd is None:
        return (
            "medium",
            "missing_password",
            ["password 为 null 或字段缺失，判定为缺失口令（按中风险处理，不得视为通过）"],
        )

    if pwd in WEAK_PASSWORDS:
        return "high", "weak_password", ["命中内置常见弱口令清单"]

    reasons: List[str] = []
    if len(pwd) < 6:
        reasons.append("口令长度不足 6 位")
    if pwd.isdigit():
        reasons.append("口令为纯数字")
    if pwd.isalpha():
        reasons.append("口令为纯字母")

    if reasons:
        return "medium", "simple_or_short_password", reasons

    return "low", "compliant", ["未命中弱口令特征，口令合规"]


def audit_entry(index: int, entry: Any, warnings: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    检测单条配置。损坏条目（缺 name/role、类型错误等）记录警告并返回 None，
    不影响其余条目检测。
    """

    def warn(reason: str) -> None:
        message = f"条目#{index} {reason}，已跳过该条目"
        warnings.append({"index": index, "reason": reason, "action": "skipped"})
        print(f"[WARN] {message}", file=sys.stderr)

    if not isinstance(entry, dict):
        warn("不是 JSON 对象")
        return None

    name = entry.get("name")
    role = entry.get("role")

    if not isinstance(name, str) or not name.strip():
        warn("缺少必填字段 name 或 name 非法")
        return None
    if not isinstance(role, str) or role not in VALID_ROLES:
        warn(f"缺少必填字段 role 或 role 非法（仅支持 {VALID_ROLES}）")
        return None

    # password 字段缺失与显式 null 同等对待；其他类型视为损坏数据
    if "password" not in entry or entry["password"] is None:
        pwd: Optional[str] = None
    elif not isinstance(entry["password"], str):
        warn("password 字段类型非法（应为字符串或 null）")
        return None
    else:
        pwd = entry["password"]

    risk_level, risk_category, reasons = classify_password(pwd)
    result: Dict[str, Any] = {
        "name": name,
        "role": role,
        "risk_level": risk_level,
        "risk_category": risk_category,
        "reasons": reasons,
        "masked_password": mask_password(pwd),
        "admin_account_risk": False,
    }

    if role == "admin" and risk_level in ("high", "medium"):
        result["admin_account_risk"] = True
        if risk_category == "missing_password":
            result["admin_risk_note"] = "管理员账户风险：管理员账户缺失口令"
        else:
            level_cn = "高风险" if risk_level == "high" else "中风险"
            result["admin_risk_note"] = f"管理员账户风险：管理员账户口令存在{level_cn}"

    return result


def build_report(config: List[Any]) -> Dict[str, Any]:
    warnings: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []

    for index, entry in enumerate(config):
        result = audit_entry(index, entry, warnings)
        if result is not None:
            results.append(result)

    # 先按风险等级（高->中->低），同级按 name 升序；缺失口令已归入中风险
    results.sort(key=lambda r: (RISK_ORDER[r["risk_level"]], r["name"]))

    summary = {
        "total_entries_in_input": len(config),
        "entries_audited": len(results),
        "entries_skipped": len(warnings),
        "high_risk": sum(1 for r in results if r["risk_level"] == "high"),
        "medium_risk": sum(1 for r in results if r["risk_level"] == "medium"),
        "low_risk": sum(1 for r in results if r["risk_level"] == "low"),
        "missing_password": sum(1 for r in results if r["risk_category"] == "missing_password"),
        "admin_account_risks": sum(1 for r in results if r["admin_account_risk"]),
    }

    return {
        "report_meta": {
            "tool": "local-password-baseline-audit",
            "mode": "local-deterministic",
            "weak_password_list_size": len(WEAK_PASSWORDS),
        },
        "summary": summary,
        "warnings": warnings,
        "results": results,
    }


def load_config(path: str) -> List[Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("配置文件顶层必须是 JSON 数组")
    return data


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地弱口令基线自查：读取 JSON 账户配置，输出脱敏后的分类风险报告。"
    )
    parser.add_argument("input", help="输入 JSON 配置文件路径（顶层为数组）")
    parser.add_argument(
        "-o",
        "--output",
        default="report.json",
        help="输出报告路径，缺省时在当前目录生成 report.json",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.input)
    except FileNotFoundError:
        print(f"[ERROR] 输入文件不存在: {args.input}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] 输入文件无法解析为 JSON 数组: {exc}", file=sys.stderr)
        return 2

    report = build_report(config)

    output_dir = os.path.dirname(os.path.abspath(args.output))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(
        f"[INFO] 检测完成：输入 {report['summary']['total_entries_in_input']} 条，"
        f"有效 {report['summary']['entries_audited']} 条，"
        f"跳过 {report['summary']['entries_skipped']} 条，"
        f"高风险 {report['summary']['high_risk']}，"
        f"中风险 {report['summary']['medium_risk']}，"
        f"低风险 {report['summary']['low_risk']}，"
        f"报告已写入 {args.output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
