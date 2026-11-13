# -*- coding: utf-8 -*-
"""路径逃逸检测入口。

读取一份或多份本地清单（JSON 数组，元素为 {"filename": ..., "content": ...}），
逐条判定文件名是否可能写出归档目录，输出 UTF-8 JSON 报告。

检测过程只读清单、只写报告文件本身，不会在归档目录内外创建或改写任何
被检测目标文件；越界候选仅做判定与记录。

退出码：存在风险条目时为 1，否则为 0。

报告字段（固定）：
  report_type      报告类型标识
  archive_root     归档目录（绝对路径）
  summary          汇总：total/risk/invalid/safe/sources_total/sources_missing/complete
  missing_sources  不可读来源清单：[{source, error}]
  entries          逐条结果，按风险等级排序（risk > invalid > safe），同级保持原始顺序：
    index          条目在全部输入中的原始序号（可对应回原样本）
    source         来源清单路径
    filename       原始文件名
    level          risk / invalid / safe
    reason         风险或无效原因；安全条目为 null
    before         修复前判定：{verdict: escape/inside/invalid, would_write_outside, resolved_path}
    after          修复后判定：{verdict: accept/reject, reason, archived_path}
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from archive_service import find_risk_reason, is_within  # noqa: E402

LEVEL_ORDER = {"risk": 0, "invalid": 1, "safe": 2}


def classify_entry(raw):
    """返回 (level, reason)。level ∈ {risk, invalid, safe}；safe 时 reason 为 None。"""
    if not isinstance(raw, dict):
        return "invalid", "条目格式错误：不是 JSON 对象"
    filename = raw.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return "invalid", "条目格式错误：文件名为空或缺失"
    if "content" not in raw or not isinstance(raw.get("content"), str):
        return "invalid", "条目格式错误：缺失内容字段 content"
    reason = find_risk_reason(filename)
    if reason is not None:
        return "risk", reason
    return "safe", None


def build_record(index, source, raw, archive_root):
    level, reason = classify_entry(raw)
    filename = raw.get("filename") if isinstance(raw, dict) else None
    record = {
        "index": index,
        "source": source,
        "filename": filename if isinstance(filename, str) else None,
        "level": level,
        "reason": reason,
    }
    if level == "invalid":
        record["before"] = {
            "verdict": "invalid",
            "would_write_outside": None,
            "resolved_path": None,
        }
        record["after"] = {"verdict": "reject", "reason": reason, "archived_path": None}
        return record
    resolved = os.path.realpath(os.path.join(archive_root, filename))
    outside = not is_within(archive_root, resolved)
    record["before"] = {
        "verdict": "escape" if outside else "inside",
        "would_write_outside": outside,
        "resolved_path": resolved,
    }
    if level == "risk":
        record["after"] = {"verdict": "reject", "reason": reason, "archived_path": None}
    else:
        record["after"] = {"verdict": "accept", "reason": None, "archived_path": resolved}
    return record


def load_sources(paths):
    """读取全部来源清单。不可读来源记入 missing，不影响其余来源。"""
    entries, missing = [], []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            missing.append({"source": path, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not isinstance(data, list):
            missing.append({"source": path, "error": "清单顶层不是 JSON 数组"})
            continue
        for raw in data:
            entries.append((path, raw))
    return entries, missing


def build_report(archive_root, sources):
    raw_entries, missing = load_sources(sources)
    records = [
        build_record(i, src, raw, archive_root)
        for i, (src, raw) in enumerate(raw_entries)
    ]
    # 稳定排序：同级保持清单原始顺序
    records.sort(key=lambda r: LEVEL_ORDER[r["level"]])
    summary = {
        "total": len(records),
        "risk": sum(1 for r in records if r["level"] == "risk"),
        "invalid": sum(1 for r in records if r["level"] == "invalid"),
        "safe": sum(1 for r in records if r["level"] == "safe"),
        "sources_total": len(sources),
        "sources_missing": len(missing),
        "complete": not missing,
    }
    return {
        "report_type": "archive_path_escape_detection",
        "archive_root": os.path.realpath(archive_root),
        "summary": summary,
        "missing_sources": missing,
        "entries": records,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="归档文件名路径逃逸检测")
    parser.add_argument("--archive-root", required=True, help="归档目录（授权根内）")
    parser.add_argument("--manifest", action="append", required=True,
                        help="清单文件路径，可重复指定多份")
    parser.add_argument("--out", default=None, help="报告输出路径（缺省打印到 stdout）")
    args = parser.parse_args(argv)

    report = build_report(args.archive_root, args.manifest)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    else:
        print(text)
    s = report["summary"]
    print(f"检测完成：共 {s['total']} 条，风险 {s['risk']}，无效 {s['invalid']}，"
          f"安全 {s['safe']}，缺失来源 {s['sources_missing']}", file=sys.stderr)
    return 1 if s["risk"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
