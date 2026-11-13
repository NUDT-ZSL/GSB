#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify.py — 对 logaudit.py 执行全部验收项检查，输出逐项 PASS/FAIL。"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "logaudit.py")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         (" -- " + detail) if detail and not cond else ""))


def run(args, **kw):
    return subprocess.run([sys.executable, TOOL] + args,
                          capture_output=True, **kw)


def has_raw_control(s):
    """字符串中是否含原始控制字符（ESC/C1/换行注入等；正常的 \\n 行分隔与制表符除外）。"""
    for ch in s:
        o = ord(ch)
        if ch in "\t\n":
            continue
        if o < 0x20 or o == 0x7F or 0x80 <= o <= 0x9F or ch in "\u2028\u2029\u0085":
            return True
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="logaudit_")
    # ---------- 12. 样本生成 ----------
    r = run(["gen", tmp])
    check("12 gen 子命令生成样本", r.returncode == 0)
    mal = os.path.join(tmp, "sample_malicious.log")
    clean = os.path.join(tmp, "sample_clean.log")

    # ---------- 1. 扫描输出合法 JSON 到 stdout ----------
    r = run(["scan", mal])
    report = None
    try:
        report = json.loads(r.stdout.decode("utf-8"))
        ok = r.returncode == 0
    except Exception:
        ok = False
    check("1 stdout 输出可解析 JSON 报告", ok)

    # 指定报告路径
    rpt_path = os.path.join(tmp, "report.json")
    r2 = run(["scan", mal, "--report", rpt_path])
    ok = r2.returncode == 0 and os.path.exists(rpt_path)
    if ok:
        try:
            json.load(open(rpt_path, encoding="utf-8"))
        except Exception:
            ok = False
    check("1 --report 指定路径输出合法 JSON", ok)

    issues = report["issues"]
    types = {i["type"] for i in issues}
    # ---------- 2. 三类问题与行号 ----------
    check("2 报告区分三类问题", {"newline_injection", "control_char_injection",
                               "sensitive_value_leak"} <= types, str(types))
    nl_lines = {i["line"] for i in issues if i["type"] == "newline_injection"}
    ct_lines = {i["line"] for i in issues if i["type"] == "control_char_injection"}
    sv_lines = {i["line"] for i in issues if i["type"] == "sensitive_value_leak"}
    check("2 换行注入行号正确(3,4)", nl_lines == {3, 4}, str(nl_lines))
    check("2 控制字符行号正确(5,6)", ct_lines == {5, 6}, str(ct_lines))
    check("2 敏感值行号正确(7,8,9)", sv_lines == {7, 8, 9}, str(sv_lines))
    check("2 行号从 1 开始", all(i["line"] >= 1 for i in issues))

    # ---------- 3. 多字节控制序列 + 中文不误判 ----------
    check("3 C1 多字节控制序列被识别(第6行)", 6 in ct_lines)
    zh_lines = {1, 2, 10}  # 含中文/正常的行
    bad = [i for i in issues if i["line"] in zh_lines]
    check("3 正常 UTF-8 中文行无误报", not bad, str(bad))

    # ---------- 4. 令牌掩码 ----------
    sv_snippets = [i["snippet"] for i in issues if i["type"] == "sensitive_value_leak"]
    full_tokens = ["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c",
                   "AKIAIOSFODNN7EXAMPLE"]
    check("4 识别至少两种令牌格式", len(sv_lines) >= 2)
    check("4 片段为掩码不含完整令牌",
          all(not any(t in s for t in full_tokens) for s in sv_snippets)
          and all("***" in s for s in sv_snippets), str(sv_snippets))

    # ---------- 5. 纯净日志 ----------
    r = run(["scan", clean])
    rep_clean = json.loads(r.stdout.decode("utf-8"))
    check("5 纯净日志问题列表为空且问题行数为 0",
          rep_clean["issues"] == [] and rep_clean["summary"]["problem_lines"] == 0)

    # ---------- 6. 汇总一致 ----------
    s = report["summary"]
    check("6 汇总问题行数与明细一致",
          s["problem_lines"] == len({i["line"] for i in issues}))
    check("6 按类型统计与明细一致",
          all(s["by_type"][t] == sum(1 for i in issues if i["type"] == t)
              for t in s["by_type"]))
    check("6 总行数正确", s["total_lines"] == 10, str(s["total_lines"]))

    # ---------- 7/8. 修复模式 ----------
    fixed = os.path.join(tmp, "fixed.log")
    r = run(["fix", mal, "--output", fixed])
    rep_fix = json.loads(r.stdout.decode("utf-8"))
    orig_lines = open(mal, "rb").read().split(b"\n")
    fixed_lines = open(fixed, "rb").read().split(b"\n")
    check("7 修复后行数不变", len(orig_lines) == len(fixed_lines))
    problem_lines = {i["line"] for i in issues}
    same = all(orig_lines[n - 1] == fixed_lines[n - 1]
               for n in range(1, len(orig_lines) + 1) if n not in problem_lines)
    check("7 普通行逐字节一致", same)
    diff = all(orig_lines[n - 1] != fixed_lines[n - 1] for n in problem_lines)
    check("7 问题行已被脱敏修改", diff)
    rem = rep_fix["remediation"]
    check("8 修复前问题数>0", rem["before"]["problem_lines"] > 0)
    check("8 修复后问题数=0", rem["after"]["problem_lines"] == 0
          and all(v == 0 for v in rem["after"]["by_type"].values()),
          json.dumps(rem["after"]))
    # 修复后文件不含原始控制字符与完整令牌
    fixed_text = open(fixed, "rb").read().decode("utf-8")
    check("8 修复后文件无原始控制字符", not has_raw_control(fixed_text))
    check("8 修复后文件无完整令牌",
          not any(t in fixed_text for t in full_tokens))

    # ---------- 9. 文件不存在 ----------
    r = run(["scan", os.path.join(tmp, "no_such_file.log")])
    check("9 不存在文件: 退出码非0", r.returncode != 0, str(r.returncode))
    check("9 不存在文件: stderr 有提示", len(r.stderr.strip()) > 0)
    check("9 不存在文件: stdout 无报告", r.stdout.strip() == b"")

    # ---------- 10/11/3. 边界样本：超长行 + 非法编码 + C1 + 中文 ----------
    edge = os.path.join(tmp, "edge.log")
    long_line = ("2026-11-14 INFO  " + "x" * 10050).encode("utf-8")
    bad_bytes = b"2026-11-14 INFO  invalid=\xff\xfe\xc0\xaf end"
    c1_line = "2026-11-14 INFO  c1=\u009b[2J done".encode("utf-8")
    zh_line = "2026-11-14 INFO  用户=张三 操作=导出报表 结果=成功".encode("utf-8")
    tok_line = b"2026-11-14 DEBUG key=AKIAIOSFODNN7EXAMPLE"
    with open(edge, "wb") as f:
        f.write(b"\n".join([zh_line, long_line, bad_bytes, c1_line, tok_line]) + b"\n")
    r = run(["scan", edge])
    ok = r.returncode == 0
    rep_edge = None
    if ok:
        try:
            rep_edge = json.loads(r.stdout.decode("utf-8"))
        except Exception:
            ok = False
    check("11 含非法编码字节不中断并输出报告", ok)
    if rep_edge:
        warns = rep_edge["warnings"]
        check("10 超长行(第2行)列为警告",
              any(w["line"] == 2 for w in warns), json.dumps(warns, ensure_ascii=False))
        ei = rep_edge["issues"]
        check("10 其余行检测不受影响(第5行令牌仍识别)",
              any(i["line"] == 5 and i["type"] == "sensitive_value_leak" for i in ei))
        check("3 C1 多字节控制序列识别(第4行)",
              any(i["line"] == 4 and i["type"] == "control_char_injection" for i in ei))
        check("3 纯中文行(第1行)无误报",
              not any(i["line"] == 1 for i in ei))
        check("11 非法编码字节被标记(第3行)",
              any(i["line"] == 3 for i in ei))
        # ---------- 13. 报告无原始控制字符 ----------
        all_snip = [i["snippet"] for i in rep_edge["issues"] + report["issues"]]
        all_snip += [w["message"] for w in rep_edge["warnings"]]
        check("13 片段/警告文本无原始控制字符",
              not any(has_raw_control(x) for x in all_snip))
        raw_out = r.stdout.decode("utf-8")
        check("13 报告输出不含 ESC/换行注入字符",
              "\x1b" not in raw_out and "\u2028" not in raw_out and "\u0085" not in raw_out)

    # ---------- 汇总 ----------
    failed = [n for n, ok, _ in results if not ok]
    print("\n==== %d/%d 项通过 ====" % (len(results) - len(failed), len(results)))
    if failed:
        print("失败项:")
        for n in failed:
            print("  -", n)
        return 1
    print("全部验收项通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
