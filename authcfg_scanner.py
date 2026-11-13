#!/usr/bin/env python3
"""authcfg_scanner.py — .authcfg 授权配置样本漏洞检测工具（仅标准库，只读样本）。

用法:
    python authcfg_scanner.py --samples <样本目录> --output <报告路径> [--fix]

退出码:
    0  扫描完成，报告已写出（无论是否发现漏洞）
    2  样本目录不存在、不是目录、或目录内无可处理的 .authcfg 样本

status 枚举: safe | vulnerable | malformed | skipped
"""
from __future__ import annotations

import argparse
import json
import os
import sys

STATUSES = ("safe", "vulnerable", "malformed", "skipped")
MAX_FILE_SIZE = 1024 * 1024  # 1MB，超过则 skipped

# 必需字段及类型（int 不接受 bool）
REQUIRED_FIELDS = {
    "version": int,
    "subject": str,
    "resource_path": str,
    "max_connections": int,
    "timeout_ms": int,
}

# 数值字段允许范围 [lo, hi]
NUMERIC_RANGES = {
    "version": (1, 3),
    "max_connections": (0, 1024),
    "timeout_ms": (0, 60000),
}

TOOL_NAME = "authcfg-scan"
TOOL_VERSION = "1.0.0"


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_resource_path(sample_dir: str, resource_path: str):
    """资源路径解析后若越出样本目录，返回问题描述；否则返回 None。"""
    base = os.path.realpath(sample_dir)
    candidate = os.path.realpath(os.path.join(base, resource_path))
    if candidate == base or candidate.startswith(base + os.sep):
        return None
    return (
        "path traversal: resource_path %r resolves to %r, "
        "outside sample directory %r" % (resource_path, candidate, base)
    )


def remediate_path(resource_path: str) -> str:
    """修复模式：去掉上跳与锚点，把路径收拢到样本目录内。"""
    parts = [
        seg
        for seg in resource_path.replace("\\", "/").split("/")
        if seg not in ("", ".", "..")
    ]
    return "/".join(parts) if parts else "."


def remediate_number(value: int, lo: int, hi: int) -> int:
    """修复模式：把越界数值钳制到允许区间。"""
    return max(lo, min(hi, value))


def analyze_file(path: str, sample_dir: str, fix: bool) -> dict:
    """分析单个样本，返回报告条目。任何异常都收敛为条目，绝不向上抛出。"""
    name = os.path.basename(path)
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_SIZE:
            return {
                "file": name,
                "status": "skipped",
                "reason": "file size %d bytes exceeds 1MB limit" % size,
            }

        with open(path, "rb") as fh:  # 只读
            raw = fh.read()

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"file": name, "status": "malformed",
                    "reason": "unparseable content: %s" % exc}

        if not isinstance(data, dict):
            return {"file": name, "status": "malformed",
                    "reason": "top-level value is not a JSON object"}

        # 必需字段与类型校验 -> malformed
        problems = []
        for field, typ in REQUIRED_FIELDS.items():
            if field not in data:
                problems.append("missing required field %r" % field)
            elif typ is int and not _is_int(data[field]):
                problems.append("field %r must be int, got %s"
                                % (field, type(data[field]).__name__))
            elif typ is str and not isinstance(data[field], str):
                problems.append("field %r must be str, got %s"
                                % (field, type(data[field]).__name__))
        if problems:
            return {"file": name, "status": "malformed",
                    "reason": "; ".join(problems)}
        if not data["subject"]:
            return {"file": name, "status": "malformed",
                    "reason": "field 'subject' must be a non-empty string"}
        if not data["resource_path"]:
            return {"file": name, "status": "malformed",
                    "reason": "field 'resource_path' must be a non-empty string"}

        # 安全检测 -> vulnerable（修复模式下转为修复并判 safe）
        issues = []
        remediations = []

        path_issue = check_resource_path(sample_dir, data["resource_path"])
        if path_issue:
            if fix:
                remediations.append(
                    "resource_path normalized to %r"
                    % remediate_path(data["resource_path"]))
            else:
                issues.append(path_issue)

        for field, (lo, hi) in NUMERIC_RANGES.items():
            value = data[field]
            if not (lo <= value <= hi):
                if fix:
                    remediations.append(
                        "%s clamped from %r to %d"
                        % (field, value, remediate_number(value, lo, hi)))
                else:
                    issues.append(
                        "numeric out of range: %s=%r not in [%d, %d]"
                        % (field, value, lo, hi))

        if issues:
            return {"file": name, "status": "vulnerable",
                    "reason": "; ".join(issues)}

        entry = {"file": name, "status": "safe", "reason": ""}
        if remediations:
            entry["fixed"] = "; ".join(remediations)
        return entry
    except Exception as exc:  # 单样本异常不影响整体流程
        return {"file": name, "status": "malformed",
                "reason": "unexpected analyzer error: %s" % exc}


def scan(sample_dir: str, fix: bool) -> dict:
    base = os.path.realpath(sample_dir)
    names = sorted(
        n for n in os.listdir(base)
        if n.endswith(".authcfg") and os.path.isfile(os.path.join(base, n))
    )
    results = [analyze_file(os.path.join(base, n), base, fix) for n in names]
    summary = {s: 0 for s in STATUSES}
    for entry in results:
        summary[entry["status"]] += 1
    summary["total"] = len(results)
    return {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "fix_mode": fix,
        "sample_dir": base,
        "status_enum": list(STATUSES),
        "summary": summary,
        "results": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan .authcfg samples and emit a JSON vulnerability report.")
    parser.add_argument("--samples", required=True, help="样本目录")
    parser.add_argument("--output", required=True, help="报告输出路径（UTF-8 JSON）")
    parser.add_argument("--fix", action="store_true",
                        help="修复模式：路径越界收拢、数值钳制，同类样本不再判 vulnerable")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.samples):
        print("error: sample directory not found: %s" % args.samples,
              file=sys.stderr)
        return 2

    candidates = [n for n in os.listdir(args.samples) if n.endswith(".authcfg")]
    if not candidates:
        print("error: no .authcfg samples found in: %s" % args.samples,
              file=sys.stderr)
        return 2

    report = scan(args.samples, args.fix)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    s = report["summary"]
    print("scanned %d sample(s): vulnerable=%d malformed=%d safe=%d skipped=%d"
          % (s["total"], s["vulnerable"], s["malformed"], s["safe"], s["skipped"]))
    print("report written to: %s" % os.path.abspath(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
