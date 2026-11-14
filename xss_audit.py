"""本地模板注入（XSS）检测与修复对比工具。

仅使用 Python 标准库，支持 Python 3.10+。

用法：
    python xss_audit.py <样本目录> <报告输出路径>

每个样本为一个 .json 文件，约定字段：
    template: str   含 {{key}} 占位符的模板字符串
    data:     dict  数据字典

工具对每个合法用例分别以「未修复（原样填充）」和「修复（HTML 实体转义）」
两种模式渲染，检测未修复结果中的可执行脚本片段，并对比修复效果，
最终产出业务可读的 JSON 报告。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from typing import Any

# 约定的样本字段名
TEMPLATE_FIELD = "template"
DATA_FIELD = "data"

# 占位符：{{ 键名 }}，键名允许字母、数字、下划线、点、连字符
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")

# 检测规则（均只在原始 HTML 标签内部匹配）
# 1) script 标签：<script ...>
SCRIPT_TAG_RE = re.compile(r"<\s*script[\s>]", re.IGNORECASE)
# 2) 事件属性片段：onxxx= 后必须紧跟引号或非空白属性值首字符；
#    首字符为 & 时不匹配，因此转义后的 onerror=&quot;... 不会命中
EVENT_HANDLER_RE = re.compile(
    r"on[a-z]+\s*=\s*(?:['\"]|[^\s'\"&>])", re.IGNORECASE
)

# 提取原始 HTML 标签（未被转义的 < ... >），转义后的 &lt; 不会被提取
RAW_TAG_RE = re.compile(r"<[^<>]*>")


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_raw(template: str, data: dict[str, Any]) -> str:
    """未修复模式：数据值原样填入占位符，不做任何转义。

    模板引用了数据字典中不存在的键时抛出 KeyError（由上层捕获并记为处理失败）。
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in data:
            raise KeyError(key)
        return str(data[key])

    return PLACEHOLDER_RE.sub(_replace, template)


def render_escaped(template: str, data: dict[str, Any]) -> str:
    """修复模式：对插入的数据值做 HTML 实体转义后再填入。

    转义 & < > " '，使尖括号与引号不再以原始形式出现。
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in data:
            raise KeyError(key)
        return html.escape(str(data[key]), quote=True)

    return PLACEHOLDER_RE.sub(_replace, template)


# --------------------------------------------------------------------------- #
# 检测
# --------------------------------------------------------------------------- #
def detect_xss(rendered: str) -> dict[str, Any]:
    """检测渲染结果中是否含可执行脚本片段。

    只在原始 HTML 标签内部判定：
      1. <script> 标签
      2. 事件属性片段（onxxx= 后跟引号或属性值，实体转义形态不命中）

    经过 HTML 实体转义后，标签尖括号变为 &lt; &gt;、引号变为
    &quot;/&#x27;，注入内容无法再构成原始标签或原始属性语法，
    因此修复后的输出不会命中规则。

    注意：javascript: 伪协议属于 URL 上下文风险，HTML 实体转义无法
    消除，需在赋值 href/src 前额外做协议白名单校验（见 README 说明）。
    """
    hits: list[str] = []
    for tag in RAW_TAG_RE.findall(rendered):
        if SCRIPT_TAG_RE.search(tag):
            hits.append("script标签")
        if EVENT_HANDLER_RE.search(tag):
            hits.append("事件属性")
    return {"vulnerable": bool(hits), "matched_rules": hits}


# --------------------------------------------------------------------------- #
# 样本校验
# --------------------------------------------------------------------------- #
def validate_case(payload: Any) -> tuple[bool, str]:
    """校验样本 JSON 结构。返回 (是否合法, 原因说明)。"""
    if not isinstance(payload, dict):
        return False, "样本根节点必须为JSON对象"
    if TEMPLATE_FIELD not in payload:
        return False, f"缺少字段:{TEMPLATE_FIELD}"
    if DATA_FIELD not in payload:
        return False, f"缺少字段:{DATA_FIELD}"
    if not isinstance(payload[TEMPLATE_FIELD], str):
        return False, f"字段{TEMPLATE_FIELD}类型不符,应为字符串"
    if not isinstance(payload[DATA_FIELD], dict):
        return False, f"字段{DATA_FIELD}类型不符,应为对象"
    return True, ""


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _safe_error_reason(exc: Exception) -> str:
    """生成不携带绝对路径信息的异常原因（避免样本路径泄漏进报告）。"""
    if isinstance(exc, (OSError, json.JSONDecodeError)):
        return f"{type(exc).__name__}: 无法读取或解析文件"
    return f"{type(exc).__name__}: {exc}"


def run_audit(sample_dir: str) -> dict[str, Any]:
    """扫描样本目录并执行检测/修复对比，返回报告字典。"""
    cases: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []
    notices: list[str] = []

    if not os.path.isdir(sample_dir):
        notices.append(f"样本目录不存在或不是目录: {os.path.basename(sample_dir.rstrip(os.sep)) or sample_dir}")
        return _build_report(cases, problems, notices)

    # 仅处理顶层 .json 文件；名称排序保证报告顺序确定
    entries = sorted(
        name for name in os.listdir(sample_dir)
        if name.lower().endswith(".json")
        and os.path.isfile(os.path.join(sample_dir, name))
    )

    if not entries:
        notices.append("样本目录为空或不含JSON样本文件，明细为空")

    for name in entries:
        case_id = name  # 用例标识仅使用相对文件名，不含任何路径
        record: dict[str, Any] = {
            "case_id": case_id,
            "valid": False,
            "vulnerable": False,
            "matched_rules": [],
            "fix_conclusion": "不适用",
            "raw_rendered": None,
            "fixed_rendered": None,
            "error": None,
        }
        path = os.path.join(sample_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:  # 不可读 / 编码错误 / JSON 解析失败
            reason = _safe_error_reason(exc)
            record["error"] = reason
            record["fix_conclusion"] = "处理失败"
            problems.append({"file": name, "reason": reason})
            cases.append(record)
            continue

        valid, reason = validate_case(payload)
        if not valid:
            record["error"] = reason
            record["fix_conclusion"] = "不合法用例"
            cases.append(record)
            continue

        record["valid"] = True
        template = payload[TEMPLATE_FIELD]
        data = payload[DATA_FIELD]

        try:
            raw_text = render_raw(template, data)
            fixed_text = render_escaped(template, data)
        except Exception as exc:  # 单个用例渲染异常不影响整体流程
            err = f"{type(exc).__name__}: {exc}"
            record["error"] = err
            record["fix_conclusion"] = "处理失败"
            cases.append(record)
            continue

        record["raw_rendered"] = raw_text
        record["fixed_rendered"] = fixed_text

        verdict = detect_xss(raw_text)
        record["vulnerable"] = verdict["vulnerable"]
        record["matched_rules"] = verdict["matched_rules"]

        if not verdict["vulnerable"]:
            record["fix_conclusion"] = "无漏洞无需修复"
        elif detect_xss(fixed_text)["vulnerable"]:
            record["fix_conclusion"] = "修复未生效"
        else:
            record["fix_conclusion"] = "已修复"

        cases.append(record)

    # 按用例标识升序，保证多次运行顺序一致
    cases.sort(key=lambda item: item["case_id"])
    return _build_report(cases, problems, notices)


def _build_report(
    cases: list[dict[str, Any]],
    problems: list[dict[str, str]],
    notices: list[str],
) -> dict[str, Any]:
    valid_cases = [c for c in cases if c["valid"]]
    vulnerable_cases = [c for c in valid_cases if c["vulnerable"]]
    fixed_cases = [c for c in vulnerable_cases if c["fix_conclusion"] == "已修复"]
    failed_cases = [c for c in cases if c["fix_conclusion"] == "处理失败"]

    return {
        "summary": {
            "total_records": len(cases),
            "valid_case_count": len(valid_cases),
            "invalid_case_count": len(cases) - len(valid_cases),
            "vulnerable_case_count": len(vulnerable_cases),
            "fixed_case_count": len(fixed_cases),
            "failed_case_count": len(failed_cases),
            "problem_file_count": len(problems),
        },
        "notices": notices,
        "problems": problems,
        "cases": cases,
    }


def write_report(report: dict[str, Any], output_path: str) -> None:
    """将报告以 UTF-8 JSON 写入指定路径（自动创建父目录）。"""
    parent = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认代码页可能不是 UTF-8，重配以保证中文提示正常显示
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="本地模板注入(XSS)检测与修复对比工具（纯标准库）"
    )
    parser.add_argument("sample_dir", help="样本目录路径，目录内每个 .json 为一个用例")
    parser.add_argument("report_path", help="JSON 报告输出路径")
    args = parser.parse_args(argv)

    report = run_audit(args.sample_dir)
    write_report(report, args.report_path)

    summary = report["summary"]
    print(f"报告已生成: {args.report_path}")
    print(
        "汇总: 明细 {total} 条 | 合法 {valid} | 不合法 {invalid} | "
        "漏洞 {vuln} | 已修复 {fixed} | 处理失败 {failed} | 问题文件 {problems}".format(
            total=summary["total_records"],
            valid=summary["valid_case_count"],
            invalid=summary["invalid_case_count"],
            vuln=summary["vulnerable_case_count"],
            fixed=summary["fixed_case_count"],
            failed=summary["failed_case_count"],
            problems=summary["problem_file_count"],
        )
    )
    for notice in report["notices"]:
        print(f"提示: {notice}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
