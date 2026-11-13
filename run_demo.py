# -*- coding: utf-8 -*-
"""一键端到端演示（跨平台，仅标准库）。

依次执行：
  1) 重新生成确定性样本（含漏洞/已修复/语法错误）
  2) 含漏洞目录扫描 + 全量模拟利用（自定义上传内容）
  3) 同名文件冲突模拟（验证原文件保留）
  4) 只读目录模拟（验证权限错误与非零退出码）
  5) 修复后目录重扫并输出 fixed_count/remaining_count 对比
所有产物输出到 out/demo/。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
DEMO_OUT = os.path.join(ROOT, "out", "demo")


def run(title, args, expect_code=0):
    print("\n" + "#" * 76)
    print("# " + title)
    print("# " + " ".join(args))
    print("#" * 76)
    code = subprocess.call(args, cwd=ROOT)
    print(f"[退出码] {code}")
    if code != expect_code:
        print(f"[注意] 期望退出码 {expect_code}，实际 {code}")
    return code


def main():
    os.makedirs(DEMO_OUT, exist_ok=True)
    run("步骤1 生成确定性样本", [PY, "make_samples.py"])
    run("步骤2 含漏洞扫描+模拟利用（自定义内容）", [
        PY, "pt_scan.py", "--dir", "samples/vulnerable",
        "--report", "out/demo/vuln_report.json",
        "--content", "PWNED-DEMO-CONTENT-27857",
    ])
    run("步骤3 同名文件冲突模拟（保留原文件并记录冲突）", [
        PY, "pt_scan.py", "--dir", "samples/vulnerable",
        "--report", "out/demo/conflict_report.json",
        "--conflict",
    ])
    run("步骤4 只读目录模拟（预期退出码 2）", [
        PY, "pt_scan.py", "--dir", "samples/vulnerable",
        "--report", "out/demo/readonly_report.json",
        "--read-only",
    ], expect_code=2)
    # 先存基线
    run("步骤5a 生成修复前基线", [
        PY, "pt_scan.py", "--dir", "samples/vulnerable", "--no-sim",
        "--report", "out/demo/baseline.json",
    ])
    run("步骤5b 修复后重扫+对比", [
        PY, "pt_scan.py", "--dir", "samples/fixed",
        "--report", "out/demo/fixed_report.json",
        "--baseline", "out/demo/baseline.json",
    ])
    run("附加 配置文件驱动模拟", [
        PY, "pt_scan.py", "--dir", "samples/vulnerable",
        "--report", "out/demo/config_report.json",
        "--sim-config", "examples/sim_config.example.json",
    ])
    print("\n演示完成，报告位于:", DEMO_OUT)


if __name__ == "__main__":
    main()
