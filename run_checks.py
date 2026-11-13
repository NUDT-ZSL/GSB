#!/usr/bin/env python3
"""run_checks.py — 对 13 条验收要求逐条实际运行验证并打印结果。

用法:
    python run_checks.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCANNER = os.path.join(HERE, "authcfg_scanner.py")
GENERATOR = os.path.join(HERE, "generate_samples.py")
WORKDIR = os.path.join(HERE, "demo_run")
SAMPLES = os.path.join(WORKDIR, "samples")
OUT = os.path.join(WORKDIR, "out")

RESULTS = []


def check(req_id: str, desc: str, ok: bool, detail: str = ""):
    RESULTS.append((req_id, ok))
    print("[%s] 要求%-2s %s%s" % ("PASS" if ok else "FAIL", req_id, desc,
                                 (" -- " + detail) if detail else ""))


def run_scanner(samples_dir: str, output: str, fix: bool = False):
    cmd = [sys.executable, SCANNER, "--samples", samples_dir, "--output", output]
    if fix:
        cmd.append("--fix")
    return subprocess.run(cmd, capture_output=True, text=True)


def load_report(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dir_hashes(root: str) -> dict:
    hashes = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            with open(p, "rb") as fh:
                hashes[rel] = hashlib.sha256(fh.read()).hexdigest()
    return hashes


def main() -> int:
    if os.path.isdir(WORKDIR):
        shutil.rmtree(WORKDIR)
    os.makedirs(SAMPLES)
    os.makedirs(OUT)

    # 确定性生成样本
    gen = subprocess.run([sys.executable, GENERATOR, "--dir", SAMPLES],
                         capture_output=True, text=True)
    print(gen.stdout.strip())
    assert gen.returncode == 0, gen.stderr

    hashes_before = dir_hashes(SAMPLES)

    # ---------- 主扫描 ----------
    report_path = os.path.join(OUT, "report.json")
    proc = run_scanner(SAMPLES, report_path)
    print(proc.stdout.strip())
    report = load_report(report_path)
    results = report["results"]
    by_file = {e["file"]: e for e in results}
    summary = report["summary"]

    # 要求2：畸形样本不影响其余样本，全部出现在报告中
    check("2", "畸形样本不影响其余样本，8 个样本全部出现在报告中",
          len(results) == 8 and summary["total"] == 8,
          "entries=%d" % len(results))

    # 要求3：路径越界 -> vulnerable
    e = by_file["02_path_traversal.authcfg"]
    check("3", "越界上跳路径样本判为 vulnerable 且给出依据",
          e["status"] == "vulnerable" and "path traversal" in e["reason"],
          e["reason"])

    # 要求4：数值越界（负数/超上限）-> vulnerable
    e_neg = by_file["03_numeric_negative.authcfg"]
    e_ovf = by_file["04_numeric_overflow.authcfg"]
    check("4", "数值越界样本判为 vulnerable 且给出依据",
          e_neg["status"] == "vulnerable" and "numeric out of range" in e_neg["reason"]
          and e_ovf["status"] == "vulnerable",
          e_neg["reason"])

    # 要求5：缺字段/类型不符/非法JSON -> malformed，不得判 vulnerable/safe
    m = [by_file["05_malformed_missing.authcfg"], by_file["06_malformed_type.authcfg"],
         by_file["07_malformed_json.authcfg"]]
    check("5", "缺失必需字段/类型不符/非法JSON 均判为 malformed",
          all(x["status"] == "malformed" for x in m),
          "; ".join(x["reason"] for x in m))

    # 要求6：合法样本 -> safe，不误报
    e = by_file["01_safe.authcfg"]
    check("6", "合法样本判为 safe 且不误报", e["status"] == "safe")

    # 要求7：summary 四项之和等于样本文件数
    total = summary["vulnerable"] + summary["malformed"] + summary["safe"] + summary["skipped"]
    check("7", "summary 四项之和等于样本文件数",
          total == 8 == summary["total"],
          "vulnerable=%d malformed=%d safe=%d skipped=%d"
          % (summary["vulnerable"], summary["malformed"], summary["safe"], summary["skipped"]))

    # 要求9：vulnerable 条目 reason 非空，safe 条目 reason 为空
    vuln_ok = all(e["reason"] for e in results if e["status"] == "vulnerable")
    safe_ok = all(e["reason"] == "" for e in results if e["status"] == "safe")
    check("9", "vulnerable 条目 reason 非空、safe 条目 reason 为空",
          vuln_ok and safe_ok)

    # 要求11：>1MB 样本 skipped，计入 summary，不影响其他样本
    e = by_file["08_oversize.authcfg"]
    check("11", "1.5MB 样本判为 skipped 且计入 summary",
          e["status"] == "skipped" and summary["skipped"] == 1 and len(results) == 8,
          e["reason"])

    # 要求12：status 均在枚举内，报告可解析
    enum = set(report["status_enum"])
    check("12", "报告可解析且所有 status 在枚举内",
          all(e["status"] in enum for e in results)
          and enum == {"safe", "vulnerable", "malformed", "skipped"})

    # 要求13：运行前后样本哈希一致（只读）
    hashes_after = dir_hashes(SAMPLES)
    check("13", "运行前后样本目录文件哈希一致（检测全程只读）",
          hashes_before == hashes_after,
          "%d files hashed" % len(hashes_before))

    # ---------- 要求1：文件名字典序 ----------
    order_dir = os.path.join(WORKDIR, "order_samples")
    os.makedirs(order_dir)
    for name in ("zeta.authcfg", "alpha.authcfg", "mid.authcfg"):
        with open(os.path.join(order_dir, name), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "subject": "s", "resource_path": "a.txt",
                       "max_connections": 1, "timeout_ms": 1}, fh)
    order_report = os.path.join(OUT, "order_report.json")
    run_scanner(order_dir, order_report)
    got = [e["file"] for e in load_report(order_report)["results"]]
    check("1", "3 个命名样本的报告条目严格按文件名字典序排列",
          got == ["alpha.authcfg", "mid.authcfg", "zeta.authcfg"], str(got))

    # ---------- 要求8：修复开关 ----------
    fixed_path = os.path.join(OUT, "report_fixed.json")
    run_scanner(SAMPLES, fixed_path, fix=True)
    fixed = load_report(fixed_path)
    fs = fixed["summary"]
    check("8", "开启修复后 vulnerable=0、safe 相应增加、其余分类不变",
          fs["vulnerable"] == 0
          and fs["safe"] == summary["safe"] + summary["vulnerable"]
          and fs["malformed"] == summary["malformed"]
          and fs["skipped"] == summary["skipped"],
          "fixed summary: %s" % {k: v for k, v in fs.items() if k != "total"})

    # ---------- 要求10：目录不存在 / 空目录 ----------
    empty_dir = os.path.join(WORKDIR, "empty_samples")
    os.makedirs(empty_dir)
    p1 = run_scanner(os.path.join(WORKDIR, "no_such_dir"),
                     os.path.join(OUT, "x1.json"))
    p2 = run_scanner(empty_dir, os.path.join(OUT, "x2.json"))
    check("10", "目录不存在或空目录：非零退出码且有明确提示",
          p1.returncode != 0 and "not found" in p1.stderr
          and p2.returncode != 0 and "no .authcfg samples" in p2.stderr,
          "missing->rc=%d, empty->rc=%d" % (p1.returncode, p2.returncode))

    failed = [r for r, ok in RESULTS if not ok]
    print("\n==== %d/%d 项验收通过 ====" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("未通过: %s" % ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
