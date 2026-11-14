#!/usr/bin/env python3
"""
demo_run.py — 端到端验证脚本，覆盖任务全部 10 项验收点。

场景：
  S1 标准扫描+隔离（验收 1/2/3/6/7/8/10 的基础部分）
  S2 隔离区同名冲突 -> unquarantined（验收 4）
  S3 ACL 拒绝读取 -> unreadable（验收 5）
  S4 文件被其他进程独占 -> unreadable，其余正常，退出码非 0（验收 9）
  S5 --dry-run 只检测不隔离（验收 10）
  S6 参数校验（非 dry-run 缺少 --quarantine 时退出码 2）

每个场景使用独立的确定性样本副本；所有断言失败都会记录并在最后汇总。
用法：
    python -I demo_run.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(REPO_DIR, "demo_workspace")
PYTHON = sys.executable
# -X utf8 保证子进程在任何控制台代码页下均以 UTF-8 输出（部分环境会忽略
# PYTHONIOENCODING）；-I 隔离用户站点包与当前目录，避免被植入模块劫持
BASE_CMD = [PYTHON, "-X", "utf8", "-I"]
SCANNER = os.path.join(REPO_DIR, "malware_scanner.py")
SAMPLES = os.path.join(REPO_DIR, "make_samples.py")
LOCK_HOLDER = os.path.join(REPO_DIR, "lock_holder.py")

VALID_STATUSES = {
    "clean", "malicious", "quarantined", "unquarantined", "unreadable",
}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

failures = []
checks_run = 0


def check(condition, description):
    global checks_run
    checks_run += 1
    mark = "PASS" if condition else "FAIL"
    print(f"    [{mark}] {description}")
    if not condition:
        failures.append(description)


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def fresh_workspace(name):
    base = os.path.join(WORKSPACE, name)
    if os.path.exists(base):
        shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    root = os.path.join(base, "samples")
    quarantine = os.path.join(base, "quarantine")
    report = os.path.join(base, "report.json")
    subprocess.run(BASE_CMD + [SAMPLES, "--root", root], check=True,
                   capture_output=True, text=True)
    return base, root, quarantine, report


def run_scanner(root, quarantine=None, report=None, dry_run=False,
                expect_fail=False):
    cmd = BASE_CMD + [SCANNER, "--root", root]
    if quarantine:
        cmd += ["--quarantine", quarantine]
    if report:
        cmd += ["--report", report]
    if dry_run:
        cmd += ["--dry-run"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if expect_fail:
        check(proc.returncode != 0, "退出码非 0（存在 unreadable/unquarantined）")
    else:
        check(proc.returncode == 0, f"退出码为 0（实际 {proc.returncode}）")
    if report:
        with open(report, encoding="utf-8") as handle:
            return json.load(handle), proc
    return json.loads(proc.stdout), proc


def check_report_invariants(report):
    """验收 1/8 通用不变量：字段完整、路径相对且升序、摘要与明细一致。"""
    paths = [item["path"] for item in report["files"]]
    check(paths == sorted(paths), "明细按路径字典序升序排列")
    check(all(not os.path.isabs(p) and "\\" not in p for p in paths),
          "报告路径均为相对输入根目录的 posix 形式")
    check(all(item["status"] in VALID_STATUSES for item in report["files"]),
          "所有状态字段取值合法")
    for item in report["files"]:
        if item["status"] != "unreadable":
            check(isinstance(item["sha256"], str) and len(item["sha256"]) == 64,
                  f"{item['path']} 含 64 位 hex SHA-256")
            check(isinstance(item["size"], int) and item["size"] >= 0,
                  f"{item['path']} 含字节大小字段")
    statuses = [item["status"] for item in report["files"]]
    summary = report["summary"]
    expected = {
        "total": len(report["files"]),
        "clean": statuses.count("clean"),
        "malicious": sum(
            s in ("malicious", "quarantined", "unquarantined") for s in statuses),
        "quarantined": statuses.count("quarantined"),
        "unquarantined": statuses.count("unquarantined"),
        "unreadable": statuses.count("unreadable"),
    }
    check(summary == expected, f"摘要计数与明细逐项吻合（{summary}）")


def by_path(report):
    return {item["path"]: item for item in report["files"]}


# ---------------------------------------------------------------- S1
def scenario_s1():
    section("S1 标准递归扫描 + 隔离（验收 1/2/3/6/7/8）")
    _, root, quarantine, report = fresh_workspace("s1")
    data, _ = run_scanner(root, quarantine, report)
    check_report_invariants(data)

    check(data["summary"]["total"] == 6, "共 6 个常规文件（2 个隐藏项 + 1 个符号链接被跳过）")
    records = by_path(data)
    check(not any(p.startswith(".") for p in records), "隐藏文件/目录未出现在报告中")
    check("link_to_readme.txt" not in records, "符号链接被跳过")
    check("junction_link" not in records and
          not any(p.startswith("junction_link/") for p in records),
          "目录联接 junction（重解析点）被跳过，未重复扫描其目标")

    check(records["evil_only.txt"]["status"] == "clean", "仅含 EVIL 的文件判定 clean")
    check(records["malware_only.txt"]["status"] == "clean", "仅含 MALWARE 的文件判定 clean")
    check(records["readme.txt"]["status"] == "clean", "无关键字文件判定 clean")

    empty = records["sub/deeper/empty.dat"]
    check(empty["status"] == "clean" and empty["size"] == 0, "0 字节文件判定 clean")
    check(empty["sha256"] == EMPTY_SHA256,
          f"空文件哈希为空串 SHA-256（{EMPTY_SHA256[:12]}...）")

    for evil_path in ("notes/mixed_case.txt", "sub/deeper/payload.bin"):
        rec = records[evil_path]
        check(rec["status"] == "quarantined", f"{evil_path} 双关键字判定恶意并隔离")
        check(rec["original_path"] == os.path.abspath(os.path.join(root, evil_path)),
              f"{evil_path} 记录正确原路径")
        check(not os.path.exists(os.path.join(root, evil_path)),
              f"{evil_path} 已从原位置移除")
        dest = rec["quarantined_path"]
        check(dest and os.path.exists(dest) and os.path.dirname(dest) ==
              os.path.abspath(quarantine), f"{evil_path} 出现在隔离区且路径正确")
        with open(dest, "rb") as handle:
            check(hashlib.sha256(handle.read()).hexdigest() == rec["sha256"],
                  f"{evil_path} 隔离后内容哈希与报告一致")

    for clean_path in ("readme.txt", "evil_only.txt", "malware_only.txt",
                       "sub/deeper/empty.dat"):
        check(os.path.exists(os.path.join(root, clean_path)),
              f"非恶意文件保持原位：{clean_path}")

    check(data["summary"] == {"total": 6, "clean": 4, "malicious": 2,
                              "quarantined": 2, "unquarantined": 0,
                              "unreadable": 0}, "摘要数值与预期完全一致")
    print(f"    报告文件：{report}")


# ---------------------------------------------------------------- S2
def scenario_s2():
    section("S2 隔离区同名冲突 -> unquarantined（验收 4）")
    _, root, quarantine, report = fresh_workspace("s2")
    os.makedirs(quarantine, exist_ok=True)
    placeholder = os.path.join(quarantine, "payload.bin")
    with open(placeholder, "wb") as handle:
        handle.write(b"PRE-EXISTING PLACEHOLDER")
    original = os.path.join(root, "sub", "deeper", "payload.bin")

    data, _ = run_scanner(root, quarantine, report, expect_fail=True)
    check_report_invariants(data)
    rec = by_path(data)["sub/deeper/payload.bin"]
    check(rec["status"] == "unquarantined", "冲突文件标记为 unquarantined")
    check(bool(rec["error"]), "附带错误说明")
    check(rec["quarantined_path"] is None and rec["original_path"] is None,
          "未隔离成功时隔离路径字段为 null")
    check(os.path.exists(original), "原恶意文件保留原位")
    with open(placeholder, "rb") as handle:
        check(handle.read() == b"PRE-EXISTING PLACEHOLDER",
              "隔离区预放文件未被覆盖")
    other = by_path(data)["notes/mixed_case.txt"]
    check(other["status"] == "quarantined", "其余恶意文件仍正常隔离")
    check(data["summary"]["unquarantined"] == 1, "摘要 unquarantined=1")


# ---------------------------------------------------------------- S3
def scenario_s3():
    section("S3 ACL 拒绝读取 -> unreadable（验收 5）")
    _, root, quarantine, report = fresh_workspace("s3")
    target = os.path.join(root, "evil_only.txt")
    try:
        deny = subprocess.run(["icacls", target, "/deny", "*S-1-1-0:(R)"],
                              capture_output=True, text=True)
        check(deny.returncode == 0, f"icacls 拒绝读取 ACL 设置成功：{deny.stdout.strip()}")
        data, _ = run_scanner(root, quarantine, report, expect_fail=True)
        check_report_invariants(data)
        rec = by_path(data)["evil_only.txt"]
        check(rec["status"] == "unreadable", "无读权限文件标记 unreadable")
        check(bool(rec["error"]), "附带错误描述")
        check(rec["sha256"] is None, "不可读文件哈希为 null")
        check(by_path(data)["readme.txt"]["status"] == "clean",
              "其他文件仍完成扫描与判定")
        check(data["summary"]["unreadable"] == 1, "摘要 unreadable=1")
    finally:
        # 务必恢复权限，避免影响工作区清理与后续场景
        subprocess.run(["icacls", target, "/remove:d", "*S-1-1-0"],
                       capture_output=True, text=True)
        subprocess.run(["icacls", target, "/grant:r", "*S-1-1-0:(RX)"],
                       capture_output=True, text=True)
        check(os.access(target, os.R_OK), "测试后已恢复文件读取权限")


# ---------------------------------------------------------------- S4
def scenario_s4():
    section("S4 文件被独占 -> 单文件失败不影响整体（验收 9）")
    _, root, quarantine, report = fresh_workspace("s4")
    locked = os.path.join(root, "malware_only.txt")
    holder = subprocess.Popen(
        BASE_CMD + [LOCK_HOLDER, "--file", locked, "--seconds", "30"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # 等待独占生效，并亲自验证一次
        effective = False
        for _ in range(50):
            time.sleep(0.1)
            try:
                with open(locked, "rb") as handle:
                    handle.read(1)
            except PermissionError:
                effective = True
                break
        check(effective, "独占锁已生效（扫描器打开将被拒绝）")

        data, _ = run_scanner(root, quarantine, report, expect_fail=True)
        check_report_invariants(data)
        rec = by_path(data)["malware_only.txt"]
        check(rec["status"] == "unreadable", "被独占文件标记 unreadable")
        check("PermissionError" in (rec["error"] or ""), "错误说明含 PermissionError")
        check(by_path(data)["readme.txt"]["status"] == "clean",
              "其余常规文件正常处理")
        check(by_path(data)["sub/deeper/payload.bin"]["status"] == "quarantined",
              "其余恶意文件正常隔离")
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    with open(locked, "rb") as handle:
        check(handle.read() != b"", "锁释放后原文件可正常读取且内容未受损")


# ---------------------------------------------------------------- S5
def scenario_s5():
    section("S5 --dry-run 仅检测不隔离（验收 10）")
    base, root, quarantine, report = fresh_workspace("s5")
    before = {}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as handle:
                before[rel] = hashlib.sha256(handle.read()).hexdigest()

    data, _ = run_scanner(root, quarantine, report, dry_run=True)
    check_report_invariants(data)
    check(not os.path.exists(quarantine), "dry-run 未创建隔离区目录")
    for evil_path in ("notes/mixed_case.txt", "sub/deeper/payload.bin"):
        rec = by_path(data)[evil_path]
        check(rec["status"] == "malicious", f"{evil_path} 状态准确为 malicious")
        check(rec["original_path"] is None and rec["quarantined_path"] is None,
              f"{evil_path} 隔离相关字段为 null")
        check(os.path.exists(os.path.join(root, evil_path)),
              f"{evil_path} 原文件未移动")
    check(by_path(data)["evil_only.txt"]["status"] == "clean", "正常文件仍为 clean")

    after = {}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as handle:
                after[rel] = hashlib.sha256(handle.read()).hexdigest()
    check(before == after, "扫描前后样本树文件与内容完全一致")
    check(data["summary"] == {"total": 6, "clean": 4, "malicious": 2,
                              "quarantined": 0, "unquarantined": 0,
                              "unreadable": 0}, "dry-run 摘要数值正确")


# ---------------------------------------------------------------- S6
def scenario_s6():
    section("S6 CLI 参数校验")
    _, root, _, _ = fresh_workspace("s6")
    proc = subprocess.run(
        BASE_CMD + [SCANNER, "--root", root],
        capture_output=True, text=True)
    check(proc.returncode == 2, "非 dry-run 缺少 --quarantine 时退出码为 2")
    check("--quarantine" in proc.stderr, "stderr 给出参数提示")


def main():
    if os.path.exists(WORKSPACE):
        shutil.rmtree(WORKSPACE, ignore_errors=True)
    for scenario in (scenario_s1, scenario_s2, scenario_s3,
                     scenario_s4, scenario_s5, scenario_s6):
        scenario()

    section(f"汇总：{checks_run} 项检查，{len(failures)} 项失败")
    if failures:
        for item in failures:
            print(f"  FAIL: {item}")
        return 1
    print("ALL CHECKS PASSED")
    print(f"\n各场景产物保留在：{WORKSPACE}（可直接查看 report.json 与隔离区）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
