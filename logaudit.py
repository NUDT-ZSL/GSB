#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""logaudit.py — 本地审计日志注入与敏感值检测/修复工具（仅 Python 标准库）

子命令：
  scan <日志文件> [--report 报告路径]      扫描并输出 JSON 报告（默认写 stdout）
  fix  <日志文件> --output <修复后文件> [--report 报告路径]
                                          生成脱敏修复后的日志，并给出修复前后对比报告
  gen  <目录>                              确定性生成样本日志（含三类问题）与纯净日志

退出码：0 成功；2 文件不存在/不可读等 IO 错误；1 参数错误。
"""
import argparse
import json
import os
import re
import sys

MAX_LINE_CHARS = 10000  # 超过该长度的行跳过检测并列为警告

# 换行注入字符（行内出现即视为伪造记录注入）
NEWLINE_ESCAPE = {
    "\r": "\\r",
    "\x0b": "\\x0b",
    "\x0c": "\\x0c",
    "\u0085": "\\u0085",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
}

# 敏感值（令牌）模式：至少两种预置格式
TOKEN_PATTERNS = [
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{36}")),
]

ISSUE_NEWLINE = "newline_injection"
ISSUE_CONTROL = "control_char_injection"
ISSUE_SECRET = "sensitive_value_leak"
ALL_TYPES = (ISSUE_NEWLINE, ISSUE_CONTROL, ISSUE_SECRET)


def is_control_char(ch):
    """判断是否为控制字符（含 C0、DEL、C1 多字节控制序列解码后的码位、解码失败占位符）。

    正常 UTF-8 文本（如中文 U+4E00–U+9FFF）不会命中。
    """
    o = ord(ch)
    if ch in NEWLINE_ESCAPE:
        return False  # 换行注入单独分类
    if ch == "\t":
        return False  # 制表符在日志中属正常
    if o < 0x20 or o == 0x7F:
        return True
    if 0x80 <= o <= 0x9F:  # C1 控制区，UTF-8 中为 0xC2 0x80–0x9F 多字节序列
        return True
    if ch == "�":  # 非法编码字节解码占位
        return True
    return False


def escape_text(s):
    """将文本中的控制字符转为可见转义形式，保证报告不含原始控制字符。"""
    out = []
    for ch in s:
        if ch in NEWLINE_ESCAPE:
            out.append(NEWLINE_ESCAPE[ch])
        elif ch == "�":
            out.append("\\ufffd")
        elif is_control_char(ch):
            o = ord(ch)
            out.append("\\x%02x" % o if o <= 0xFF else "\\u%04x" % o)
        else:
            out.append(ch)
    return "".join(out)


def mask_token(tok):
    """遮蔽令牌：仅保留前 4 个字符，报告中不出现完整原值。"""
    return tok[:4] + "***MASKED***"


def detect(line_no, text):
    """对单行解码文本执行检测，返回问题记录列表（每行每类型一条，含转义片段）。"""
    records = []
    # 1) 换行注入
    for i, ch in enumerate(text):
        if ch in NEWLINE_ESCAPE:
            snippet = text[max(0, i - 10):i + 11]
            records.append({
                "line": line_no,
                "type": ISSUE_NEWLINE,
                "snippet": escape_text(snippet),
            })
            break
    # 2) 控制字符注入
    for i, ch in enumerate(text):
        if is_control_char(ch):
            snippet = text[max(0, i - 10):i + 11]
            records.append({
                "line": line_no,
                "type": ISSUE_CONTROL,
                "snippet": escape_text(snippet),
            })
            break
    # 3) 敏感值泄露（片段以掩码呈现）
    for name, pat in TOKEN_PATTERNS:
        m = pat.search(text)
        if m:
            records.append({
                "line": line_no,
                "type": ISSUE_SECRET,
                "snippet": mask_token(m.group(0)),
                "pattern": name,
            })
            break
    return records


def sanitize(text):
    """脱敏修复单行：令牌遮蔽、注入字符转为字面转义序列。"""
    for _name, pat in TOKEN_PATTERNS:
        text = pat.sub(lambda m: mask_token(m.group(0)), text)
    out = []
    for ch in text:
        if ch in NEWLINE_ESCAPE:
            out.append(NEWLINE_ESCAPE[ch])  # 转义为字面文本，如 \r
        elif ch == "�":
            out.append("\\ufffd")
        elif is_control_char(ch):
            o = ord(ch)
            out.append("\\x%02x" % o if o <= 0xFF else "\\u%04x" % o)
        else:
            out.append(ch)
    return "".join(out)


def split_lines(raw):
    """按 \\n 切分；返回 (行字节列表, 是否以换行结尾)。"""
    if raw == b"":
        return [], False
    trailing = raw.endswith(b"\n")
    lines = raw.split(b"\n")
    if trailing:
        lines.pop()
    return lines, trailing


def scan_bytes(raw):
    """扫描字节内容，返回 (issues, warnings, summary)。"""
    lines, _ = split_lines(raw)
    issues, warnings = [], []
    for idx, line in enumerate(lines, 1):
        body = line[:-1] if line.endswith(b"\r") else line
        text = body.decode("utf-8", errors="replace")
        if len(text) > MAX_LINE_CHARS:
            warnings.append({
                "line": idx,
                "message": "line exceeds %d characters (%d chars), detection skipped"
                           % (MAX_LINE_CHARS, len(text)),
            })
            continue
        issues.extend(detect(idx, text))
    by_type = {t: 0 for t in ALL_TYPES}
    for rec in issues:
        by_type[rec["type"]] += 1
    summary = {
        "total_lines": len(lines),
        "problem_lines": len({r["line"] for r in issues}),
        "by_type": by_type,
        "warning_count": len(warnings),
    }
    return issues, warnings, summary


def fix_bytes(raw):
    """生成修复后的日志字节：未受影响行保持原始字节不变。"""
    lines, trailing = split_lines(raw)
    out_lines = []
    for idx, line in enumerate(lines, 1):
        has_cr = line.endswith(b"\r")
        body = line[:-1] if has_cr else line
        text = body.decode("utf-8", errors="replace")
        if len(text) > MAX_LINE_CHARS or not detect(idx, text):
            out_lines.append(line)  # 原样保留（逐字节一致）
            continue
        fixed = sanitize(text).encode("utf-8")
        if has_cr:
            fixed += b"\r"
        out_lines.append(fixed)
    result = b"\n".join(out_lines)
    if trailing:
        result += b"\n"
    return result


def read_file(path):
    """读取文件字节；失败时向 stderr 报错并以退出码 2 终止，不产生部分报告。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        sys.stderr.write("error: cannot read log file '%s': %s\n" % (path, exc))
        sys.exit(2)


def emit_report(report, report_path):
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if report_path:
        try:
            with open(report_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text + "\n")
        except OSError as exc:
            sys.stderr.write("error: cannot write report '%s': %s\n" % (report_path, exc))
            sys.exit(2)
        sys.stderr.write("report written to %s\n" % report_path)
    else:
        sys.stdout.write(text + "\n")


def cmd_scan(args):
    raw = read_file(args.logfile)
    issues, warnings, summary = scan_bytes(raw)
    emit_report({
        "tool": "logaudit",
        "mode": "scan",
        "file": args.logfile,
        "summary": summary,
        "issues": issues,
        "warnings": warnings,
    }, args.report)


def cmd_fix(args):
    raw = read_file(args.logfile)
    issues, warnings, before = scan_bytes(raw)
    fixed = fix_bytes(raw)
    try:
        with open(args.output, "wb") as f:
            f.write(fixed)
    except OSError as exc:
        sys.stderr.write("error: cannot write fixed log '%s': %s\n" % (args.output, exc))
        sys.exit(2)
    _i2, _w2, after = scan_bytes(fixed)  # 对修复结果复检
    emit_report({
        "tool": "logaudit",
        "mode": "fix",
        "file": args.logfile,
        "fixed_file": args.output,
        "summary": before,
        "issues": issues,
        "warnings": warnings,
        "remediation": {
            "before": {"problem_lines": before["problem_lines"], "by_type": before["by_type"]},
            "after": {"problem_lines": after["problem_lines"], "by_type": after["by_type"]},
        },
    }, args.report)


def cmd_gen(args):
    """确定性生成样本：含三类问题的日志与纯净日志。"""
    out_dir = args.dir
    os.makedirs(out_dir, exist_ok=True)
    jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
           ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
           ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c")
    aws = "AKIAIOSFODNN7EXAMPLE"
    ghp = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"[:36]
    malicious_lines = [
        "2026-11-14 09:00:01 INFO  user=alice action=login result=success",
        "2026-11-14 09:00:02 INFO  user=bob action=view 资源=报表 result=success",
        # 换行注入：行内 \r 与 U+2028 伪造记录
        "2026-11-14 09:00:03 INFO  user=mallory action=login result=success\r"
        "2026-11-14 09:00:03 INFO  user=admin action=delete result=success",
        "2026-11-14 09:00:04 INFO  msg=正常操作\u2028action=grant role=admin",
        # 控制字符注入：ANSI 转义序列、响铃、C1 控制（CSI，UTF-8 多字节）
        "2026-11-14 09:00:05 INFO  msg=\x1b[31m伪造红色告警\x1b[0m done",
        "2026-11-14 09:00:06 INFO  msg=bell\x07backspace\x08 c1=\u009b done",
        # 敏感值泄露：JWT、AWS Access Key、GitHub Token
        "2026-11-14 09:00:07 DEBUG token=" + jwt,
        "2026-11-14 09:00:08 DEBUG aws_key=" + aws,
        "2026-11-14 09:00:09 DEBUG github_token=" + ghp,
        "2026-11-14 09:00:10 INFO  user=carol action=logout result=success",
    ]
    clean_lines = [
        "2026-11-14 09:00:01 INFO  user=alice action=login result=success",
        "2026-11-14 09:00:02 INFO  user=bob action=view 资源=报表 result=success",
        "2026-11-14 09:00:03 INFO  user=carol action=export 行数=128 result=success",
        "2026-11-14 09:00:04 INFO  user=dave action=logout result=success",
    ]
    p_mal = os.path.join(out_dir, "sample_malicious.log")
    p_clean = os.path.join(out_dir, "sample_clean.log")
    with open(p_mal, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(malicious_lines) + "\n")
    with open(p_clean, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(clean_lines) + "\n")
    sys.stdout.write("generated:\n  %s\n  %s\n" % (p_mal, p_clean))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="logaudit",
        description="本地审计日志注入与敏感值检测/修复工具（仅标准库）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="扫描日志并输出 JSON 报告")
    p_scan.add_argument("logfile", help="日志文件路径")
    p_scan.add_argument("--report", help="报告输出路径（缺省写标准输出）")
    p_scan.set_defaults(func=cmd_scan)

    p_fix = sub.add_parser("fix", help="生成脱敏修复后的日志并输出对比报告")
    p_fix.add_argument("logfile", help="日志文件路径")
    p_fix.add_argument("--output", required=True, help="修复后日志输出路径")
    p_fix.add_argument("--report", help="报告输出路径（缺省写标准输出）")
    p_fix.set_defaults(func=cmd_fix)

    p_gen = sub.add_parser("gen", help="确定性生成样本日志")
    p_gen.add_argument("dir", help="样本输出目录")
    p_gen.set_defaults(func=cmd_gen)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
