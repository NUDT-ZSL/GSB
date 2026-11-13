# -*- coding: utf-8 -*-
"""一键验证入口：生成确定性样本，运行检测与修复验证，逐项输出结果。

退出码：全部检查通过为 0，任一失败为 1。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

import generate_samples  # noqa: E402
from archive_service import (  # noqa: E402
    ArchiveRejected,
    FixedArchiver,
    VulnerableArchiver,
    is_within,
)

TESTROOT = os.path.join(ROOT, "testroot")
ARCHIVE = os.path.join(TESTROOT, "archive")
REPORTS = os.path.join(TESTROOT, "reports")
MANIFESTS = os.path.join(TESTROOT, "manifests")
MIXED = os.path.join(MANIFESTS, "mixed.json")
ALL_SAFE = os.path.join(MANIFESTS, "all_safe.json")
EXTRA = os.path.join(MANIFESTS, "extra.json")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))


def run_detector(manifest_paths, out_path):
    cmd = [sys.executable, os.path.join(SRC, "detector.py"),
           "--archive-root", ARCHIVE, "--out", out_path]
    for m in manifest_paths:
        cmd.extend(["--manifest", m])
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    return proc.returncode


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find(records, filename):
    for r in records:
        if r["filename"] == filename:
            return r
    return None


def tree_snapshot(root, exclude_top):
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            if rel.split(os.sep)[0] in exclude_top:
                continue
            with open(p, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            snap[rel] = (digest, os.path.getmtime(p))
    return snap


def main():
    print("== 步骤 1/7 生成确定性样本 ==")
    generate_samples.generate(TESTROOT)
    print(f"测试根目录：{TESTROOT}")

    print("\n== 步骤 2/7 检测混合清单（含风险/无效/安全） ==")
    report_mixed_path = os.path.join(REPORTS, "report_mixed.json")
    rc_mixed = run_detector([MIXED], report_mixed_path)
    report = load_json(report_mixed_path)
    entries = report["entries"]
    summary = report["summary"]

    # 需求 1：父目录跳转判风险，普通文件名判安全
    r_escape = find(entries, "..\\escape.txt")
    r_readme = find(entries, "readme.txt")
    check("需求1 父目录跳转样本判为风险",
          r_escape is not None and r_escape["level"] == "risk",
          f"level={r_escape and r_escape['level']}, reason={r_escape and r_escape['reason']}")
    check("需求1 普通文件名判为安全",
          r_readme is not None and r_readme["level"] == "safe"
          and r_readme["reason"] is None)

    # 需求 10：存在风险时退出码非零
    check("需求10 含风险样本时检测退出码非零", rc_mixed != 0, f"exit={rc_mixed}")

    # 需求 4：按风险等级排序，同级保持原始顺序
    order = {"risk": 0, "invalid": 1, "safe": 2}
    levels = [order[r["level"]] for r in entries]
    sorted_ok = all(a <= b for a, b in zip(levels, levels[1:]))
    stable_ok = True
    for lvl in ("risk", "invalid", "safe"):
        idx = [r["index"] for r in entries if r["level"] == lvl]
        stable_ok = stable_ok and idx == sorted(idx)
    check("需求4 风险项在前、安全项在后，同级保持原始顺序",
          sorted_ok and stable_ok,
          f"报告顺序 index={[r['index'] for r in entries]}")

    # 需求 6：每条含修复前与修复后两个结论字段
    check("需求6 每条含 before/after 两个结论字段",
          all("before" in r and "after" in r
              and "verdict" in r["before"] and "verdict" in r["after"]
              for r in entries))

    # 需求 7：无效类单独统计，其他条目不受影响
    invalid_names = {r["filename"] for r in entries if r["level"] == "invalid"}
    check("需求7 空文件名与缺失内容字段归入无效类并单独统计",
          summary["invalid"] == 2 and invalid_names == {"", "no_content.txt"}
          and summary["risk"] == 4 and summary["safe"] == 4,
          f"summary={summary}")

    # 需求 9：盘符与绝对路径判为越界
    r_drive = find(entries, "C:\\Windows\\Temp\\evil.txt")
    r_abs = find(entries, "/etc/shadow")
    check("需求9 盘符与绝对路径样本判为越界",
          r_drive["level"] == "risk" and r_drive["before"]["would_write_outside"] is True
          and r_abs["level"] == "risk" and r_abs["before"]["would_write_outside"] is True)

    # 需求 11：UTF-8 JSON 可解析，安全项原因 null、风险项为字符串
    check("需求11 报告为合法 JSON，安全项 reason 为 null、风险项为字符串",
          all(r["reason"] is None for r in entries if r["level"] == "safe")
          and all(isinstance(r["reason"], str) for r in entries if r["level"] == "risk"))

    print("\n== 步骤 3/7 校验检测过程未触碰归档目录外文件 ==")
    # 需求 2：诱饵文件内容与修改时间保持不变
    decoys_ok = True
    for rel, expected in generate_samples.DECOYS.items():
        p = os.path.join(TESTROOT, rel)
        with open(p, "r", encoding="utf-8") as f:
            content_ok = f.read() == expected
        mtime_ok = os.path.getmtime(p) == generate_samples.FIXED_MTIME
        decoys_ok = decoys_ok and content_ok and mtime_ok
    check("需求2 检测后归档目录外诱饵内容/修改时间不变", decoys_ok)

    print("\n== 步骤 4/7 全安全清单检测 ==")
    # 需求 10：全部安全时退出码为零
    rc_safe = run_detector([ALL_SAFE], os.path.join(REPORTS, "report_safe.json"))
    check("需求10 全部安全时检测退出码为零", rc_safe == 0, f"exit={rc_safe}")

    print("\n== 步骤 5/7 删除一份清单来源后检测 ==")
    # 需求 8：来源缺失时其余条目正常出结果，缺失来源被明确列出
    os.remove(EXTRA)
    run_detector([MIXED, EXTRA], os.path.join(REPORTS, "report_partial.json"))
    partial = load_json(os.path.join(REPORTS, "report_partial.json"))
    missing_listed = any(m["source"] == EXTRA for m in partial["missing_sources"])
    check("需求8 缺失来源被明确列出，其余条目正常出结果",
          missing_listed and partial["summary"]["complete"] is False
          and len(partial["entries"]) == len(generate_samples.MIXED)
          and partial["summary"]["risk"] == 4,
          f"missing={partial['missing_sources']}")

    print("\n== 步骤 6/7 修复后组件归档与拒绝 ==")
    snap_before = tree_snapshot(TESTROOT, exclude_top={"archive"})
    fixer = FixedArchiver(ARCHIVE)

    # 需求 3：中文、空格、多级扩展名归档成功且内容一致
    all_safe_entries = load_json(ALL_SAFE)
    archive_ok = True
    details = []
    for e in all_safe_entries:
        path = fixer.archive(e["filename"], e["content"])
        with open(path, "r", encoding="utf-8") as f:
            same = f.read() == e["content"]
        archive_ok = archive_ok and same and is_within(ARCHIVE, path)
        details.append(f"{e['filename']} -> {'OK' if same else 'MISMATCH'}")
    check("需求3 中文/空格/多级扩展名归档成功且内容一致", archive_ok,
          "; ".join(details))

    # 需求 5：越界样本被拒绝且给出可读原因
    reason_text = ""
    rejected = False
    try:
        fixer.archive("..\\..\\evil.txt", "evil")
    except ArchiveRejected as exc:
        rejected = True
        reason_text = str(exc)
    check("需求5 父目录跳转样本被拒绝并给出可读原因",
          rejected and len(reason_text) > 0, f"拒绝原因：{reason_text}")

    # 需求 5/9：归档目录外无新文件、无改写
    snap_after = tree_snapshot(TESTROOT, exclude_top={"archive"})
    check("需求5/9 修复后操作在归档目录外无新文件、无改写",
          snap_before == snap_after)

    print("\n== 步骤 7/7 缺陷实证（沙箱内运行修复前组件） ==")
    # 在 testroot/sandbox 内实证修复前组件确实会逃逸（仍在授权测试根内）
    sandbox_archive = os.path.join(TESTROOT, "sandbox", "archive")
    VulnerableArchiver(sandbox_archive).archive(os.path.join("..", "pwned.txt"), "pwned")
    escaped_file = os.path.join(TESTROOT, "sandbox", "pwned.txt")
    check("缺陷实证 修复前组件将文件写到了归档目录之外",
          os.path.exists(escaped_file), f"逃逸文件：{escaped_file}")
    fixed_sandbox = FixedArchiver(os.path.join(TESTROOT, "sandbox", "archive_fixed"))
    blocked = False
    try:
        fixed_sandbox.archive(os.path.join("..", "pwned2.txt"), "pwned")
    except ArchiveRejected:
        blocked = True
    check("缺陷实证 同一样本被修复后组件拒绝",
          blocked and not os.path.exists(os.path.join(TESTROOT, "sandbox", "pwned2.txt")))

    print("\n== 验证结果汇总 ==")
    failed = [n for n, ok in RESULTS if not ok]
    print(f"共 {len(RESULTS)} 项检查，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    if failed:
        for n in failed:
            print(f"  失败：{n}")
    print(f"报告目录：{REPORTS}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
