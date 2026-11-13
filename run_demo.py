#!/usr/bin/env python3
"""端到端演示：生成样本 → 修复前扫描 → 修复后复跑对比 → 篡改计数 → 演示退出码。

运行::

    python -I run_demo.py            # 使用 ./demo_out 与 ./reports
    python -I run_demo.py --clean    # 先删除旧输出再重来

脚本只调用 upload_audit.py 的公开 CLI，不修改任何被扫描文件。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(HERE, "upload_audit.py")
MAKE_SAMPLES = os.path.join(HERE, "make_samples.py")


def _configure():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def run(label, args, *, expect=None):
    """运行一条命令并打印摘要；返回 CompletedProcess。"""
    cmd = [sys.executable, "-I", *args]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=HERE, env=env,
    )
    print("\n" + "=" * 78)
    print(f"$ {label}")
    print("  " + " ".join(['python', '-I'] + [os.path.relpath(p, HERE) if os.path.isabs(p) else p for p in args]))
    for line in (proc.stdout.strip().splitlines() + proc.stderr.strip().splitlines()):
        print("  " + line)
    print(f"  [退出码 {proc.returncode}]")
    if expect is not None and proc.returncode != expect:
        raise SystemExit(
            f"演示失败：{label} 期望退出码 {expect}，实际 {proc.returncode}"
        )
    return proc


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _force_remove(path):
    """删除目录；若含目录链接/联接，只删除链接本身。"""
    if not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path):
        for name in list(dirs):
            child = os.path.join(root, name)
            if os.path.islink(child) or _is_reparse(child):
                try:
                    os.unlink(child)
                except OSError:
                    os.rmdir(child)
                dirs.remove(name)
    shutil.rmtree(path, ignore_errors=True)


def _is_reparse(path):
    if sys.platform != "win32":
        return False
    try:
        import stat as stat_module

        return bool(
            os.lstat(path).st_file_attributes & stat_module.FILE_ATTRIBUTE_REPARSE_POINT
        )
    except (AttributeError, OSError):
        return False


def main(argv=None):
    _configure()
    parser = argparse.ArgumentParser(description="上传核验工具端到端演示")
    parser.add_argument("--out", default="demo_out", help="样本目录（默认 ./demo_out）")
    parser.add_argument("--reports", default="reports", help="报告目录（默认 ./reports）")
    parser.add_argument("--clean", action="store_true", help="先删除旧样本与报告")
    args = parser.parse_args(argv)

    out = os.path.join(HERE, args.out)
    reports = os.path.join(HERE, args.reports)
    if args.clean:
        shutil.rmtree(reports, ignore_errors=True)
        _force_remove(out)
    os.makedirs(reports, exist_ok=True)

    before_upload = os.path.join(out, "before", "upload")
    before_manifest = os.path.join(out, "before", "manifest.csv")
    after_upload = os.path.join(out, "after", "upload")
    after_manifest = os.path.join(out, "after", "manifest.csv")
    before_report = os.path.join(reports, "before.json")
    after_report = os.path.join(reports, "after.json")
    compare_report = os.path.join(reports, "compare.json")
    marker = os.path.join(out, "EXECUTED_MARKER.txt")

    print("步骤 1/7：确定性生成样本（修复前 / 修复后 / 回归）")
    run("生成样本", [MAKE_SAMPLES, "--out", args.out, "--force"], expect=0)

    print("\n步骤 2/7：不带参数运行，应打印用法并以非零码退出")
    run("缺少参数", [AUDIT], expect=2)

    print("\n步骤 3/7：扫描修复前上传目录")
    run(
        "修复前扫描",
        [AUDIT, before_upload, before_manifest, "--report", before_report],
        expect=0,
    )
    before = load(before_report)
    print("\n  修复前计数:", json.dumps(before["counts"], ensure_ascii=False))
    for item in before["results"]:
        if item["status"] == "存在风险":
            print(
                "    - {!r} -> {}（{}）".format(
                    item["relative_path"],
                    item["risk_types"],
                    item["reasons"][-1],
                )
            )

    print("\n步骤 4/7：确认可执行样本没有被运行（标记文件必须不存在）")
    print(f"  {marker}")
    print("  不存在" if not os.path.exists(marker) else "  !! 存在 —— 扫描执行了上传文件")
    if os.path.exists(marker):
        raise SystemExit("演示失败：扫描执行了上传文件")

    print("\n步骤 5/7：扫描修复后上传目录并与修复前报告对比")
    run(
        "修复后扫描 + 对比",
        [
            AUDIT, after_upload, after_manifest,
            "--report", after_report,
            "--baseline", before_report,
            "--compare-report", compare_report,
        ],
        expect=0,
    )
    comparison = load(compare_report)
    print("\n  对比汇总:", json.dumps(comparison["summary"], ensure_ascii=False))
    print("  已消除风险：")
    for entry in comparison["已消除风险"]:
        print(f"    - {entry['relative_path']}  [{entry['risk_label']}]")
    print("  新增风险：")
    for entry in comparison["新增风险"] or []:
        print(f"    - {entry['relative_path']}  [{entry['risk_label']}]")
    if not comparison["已消除风险"]:
        raise SystemExit("演示失败：期望存在已消除风险")

    print("\n步骤 6/7：人为篡改计数后重跑，应拒绝覆盖旧报告（退出码 3）")
    tampered_path = os.path.join(reports, "after_tampered.json")
    tampered = load(after_report)
    tampered["counts"]["安全"] += 3
    with open(tampered_path, "w", encoding="utf-8") as handle:
        json.dump(tampered, handle, ensure_ascii=False, indent=2)
    run("校验被篡改的报告", [AUDIT, "--validate-report", tampered_path], expect=3)
    with open(tampered_path, "rb") as handle:
        snapshot = handle.read()
    run(
        "重跑且拒绝覆盖",
        [AUDIT, after_upload, after_manifest, "--report", tampered_path],
        expect=3,
    )
    with open(tampered_path, "rb") as handle:
        if handle.read() != snapshot:
            raise SystemExit("演示失败：旧报告被覆盖")
    print("  旧报告字节未变化 ✓")

    print("\n步骤 7/7：上传目录不存在时应报错且不产生报告")
    ghost_report = os.path.join(reports, "should_not_exist.json")
    run(
        "上传目录不存在",
        [AUDIT, os.path.join(out, "no_such_dir"), before_manifest,
         "--report", ghost_report],
        expect=4,
    )
    if os.path.exists(ghost_report):
        raise SystemExit("演示失败：错误场景不应产生报告文件")
    print("  未产生报告文件 ✓")

    print("\n" + "=" * 78)
    print("演示全部通过。报告位于:", reports)
    print("  before.json     修复前扫描")
    print("  after.json      修复后扫描")
    print("  compare.json    修复前/后对比（新增风险 / 已消除风险）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
