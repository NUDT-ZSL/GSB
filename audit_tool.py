#!/usr/bin/env python3
"""本地 Python 代码库安全审计工具（仅使用标准库）。

用法:
    python audit_tool.py <审计目录> <规则文件.json> <输出报告.json> [--exclude a,b,c]

报告结构:
    {
      "summary":  {"files_audited": int, "findings_count": int},
      "findings": [{"file", "line", "column", "rule_id", "severity", "fix"}, ...],
      "warnings": [str, ...]
    }
findings 按 (file, line, column) 升序排序；file 为相对审计目录的路径（统一用 / 分隔）。
"""
import argparse
import json
import os
import re
import sys

VALID_SEVERITIES = ("高", "中", "低")
REQUIRED_FIELDS = ("id", "pattern", "severity", "fix")


def load_rules(rules_path, warnings):
    """加载规则文件。非法规则不生效并记录告警，返回有效规则列表。"""
    with open(rules_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("rules", [])
    if not isinstance(data, list):
        raise ValueError("规则文件顶层必须是数组或含 rules 数组的对象")

    rules = []
    for idx, item in enumerate(data):
        label = "规则#%d" % idx
        if not isinstance(item, dict):
            warnings.append("%s 不是对象，已忽略" % label)
            continue
        rid = item.get("id", "<无id:%d>" % idx)
        label = "规则 %s" % rid
        # 字段缺失或为空 -> 不生效
        missing = [k for k in REQUIRED_FIELDS
                   if k not in item or not isinstance(item[k], str) or not item[k]]
        if missing:
            warnings.append("%s 缺少必填字段(%s)，该规则不生效"
                            % (label, ",".join(missing)))
            continue
        # severity 非法 -> 不生效
        if item["severity"] not in VALID_SEVERITIES:
            warnings.append("%s severity 非法(%r)，仅允许 高/中/低，该规则不生效"
                            % (label, item["severity"]))
            continue
        # 正则无效 -> 不生效
        try:
            rx = re.compile(item["pattern"])
        except re.error as e:
            warnings.append("%s 正则表达式无效(%s)，该规则不生效" % (label, e))
            continue
        rules.append({"id": item["id"], "regex": rx,
                      "severity": item["severity"], "fix": item["fix"]})
    return rules


def _norm(p):
    return p.replace(os.sep, "/")


def make_excluder(root, exclude_arg):
    """根据 --exclude 逗号分隔列表生成排除判断函数。

    条目可匹配：相对路径本身、相对路径前缀（目录）、任一路径分量名。
    """
    entries = [e.strip().strip("/").replace("\\", "/")
               for e in (exclude_arg or "").split(",") if e.strip()]

    def excluded(rel):
        rel = _norm(rel)
        parts = rel.split("/")
        for e in entries:
            if rel == e or rel.startswith(e + "/") or e in parts:
                return True
        return False

    return excluded


def iter_py_files(root, excluded):
    """确定性顺序遍历未排除的 .py 文件，产出 (相对路径, 绝对路径)。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not excluded(os.path.relpath(os.path.join(dirpath, d), root)))
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            ap = os.path.join(dirpath, fn)
            rel = os.path.relpath(ap, root)
            if excluded(rel):
                continue
            yield _norm(rel), ap


def audit(root, rules, excluded, warnings):
    """执行审计，返回 (files_audited, findings)。"""
    findings = []
    files_audited = 0
    for rel, ap in iter_py_files(root, excluded):
        try:
            with open(ap, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError) as e:
            warnings.append("文件 %s 读取失败(%s)，已跳过" % (rel, e))
            continue
        files_audited += 1
        for lineno, line in enumerate(lines, start=1):
            for rule in rules:
                for m in rule["regex"].finditer(line):
                    findings.append({
                        "file": rel,
                        "line": lineno,
                        "column": m.start() + 1,
                        "rule_id": rule["id"],
                        "severity": rule["severity"],
                        "fix": rule["fix"],
                    })
    findings.sort(key=lambda f: (f["file"], f["line"], f["column"]))
    return files_audited, findings


def run(audit_dir, rules_path, out_path, exclude=None):
    if not os.path.isdir(audit_dir):
        print("错误: 审计目录不存在或不是目录: %s" % audit_dir, file=sys.stderr)
        return 2
    if not os.path.isfile(rules_path):
        print("错误: 规则文件不存在: %s" % rules_path, file=sys.stderr)
        return 2

    warnings = []
    try:
        rules = load_rules(rules_path, warnings)
    except (OSError, ValueError) as e:
        print("错误: 规则文件无法解析: %s" % e, file=sys.stderr)
        return 2

    excluded = make_excluder(audit_dir, exclude)
    files_audited, findings = audit(audit_dir, rules, excluded, warnings)

    report = {
        "summary": {"files_audited": files_audited,
                    "findings_count": len(findings)},
        "findings": findings,
        "warnings": warnings,
    }
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("审计完成: 文件 %d 个, 缺陷 %d 条, 告警 %d 条 -> %s"
          % (files_audited, len(findings), len(warnings), out_path))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="本地 Python 代码库安全审计工具")
    p.add_argument("audit_dir", help="待审计目录")
    p.add_argument("rules", help="规则 JSON 文件路径")
    p.add_argument("output", help="输出报告 JSON 路径")
    p.add_argument("--exclude", default="",
                   help="逗号分隔的要排除的目录或文件（相对路径或名称）")
    args = p.parse_args(argv)
    return run(args.audit_dir, args.rules, args.output, args.exclude)


if __name__ == "__main__":
    sys.exit(main())
