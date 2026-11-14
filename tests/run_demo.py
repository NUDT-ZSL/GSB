#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端验证编排器：构建夹具 → 启动占锁 → 逐条核对 11 项验收点。

用法（在项目根目录）:
    python -I tests/run_demo.py
所有子进程均以 -I（隔离模式）启动，避免从夹具目录加载到同名模块。
"""

import json
import os
import subprocess
import sys
import time

# 中文 Windows 控制台默认 GBK，强制 UTF-8 避免报告中的中文/特殊字符解码失败
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
WORKSPACE = os.path.join(TESTS, "workspace")
TREE = os.path.join(WORKSPACE, "sample_tree")
TOOL = os.path.join(ROOT, "file_audit.py")

sys.path.insert(0, ROOT)
import file_audit  # noqa: E402  复用遍历器做结构断言

# 子 Python 进程统一使用 UTF-8 输出
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8",
             "PYTHONUTF8": "1"}

PASS_COUNT = 0
FAIL_COUNT = 0


def check(desc: str, ok: bool, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    mark = "PASS" if ok else "FAIL"
    if ok:
        PASS_COUNT += 1
    else:
        FAIL_COUNT += 1
    print(f"[{mark}] {desc}" + (f" -> {detail}" if detail and not ok else ""))


def run_tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", TOOL, *args],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=CHILD_ENV,
    )


def rule_path(name: str) -> str:
    return os.path.join(WORKSPACE, name)


def main() -> int:
    print("== 步骤 0：构建确定性夹具 ==")
    build = subprocess.run(
        [sys.executable, "-I", os.path.join(TESTS, "build_fixture.py"),
         WORKSPACE],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=CHILD_ENV,
    )
    print(build.stdout.strip())
    if build.returncode != 0:
        print(build.stderr)
        return 1
    link_kind = "junction" if "LINK_KIND=junction" in build.stdout \
        else "symbolic_link"

    locked_file = os.path.join(TREE, "locked", "locked.txt")
    print("\n== 步骤 1：启动占锁进程（共享冲突场景） ==")
    locker = subprocess.Popen(
        [sys.executable, "-I", os.path.join(TESTS, "lock_holder.py"),
         locked_file, "30"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", env=CHILD_ENV,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        line = locker.stdout.readline()
        if line.startswith("LOCKED"):
            print(line.strip())
            break
    else:
        print("FAIL: 占锁进程未在 10 秒内就绪")
        return 1

    try:
        print("\n== 验收 1/11：正常扫描，退出码 0，报告可解析 ==")
        proc = run_tool(TREE, rule_path("rules_valid.json"))
        check("正常扫描退出码为 0", proc.returncode == 0,
              f"rc={proc.returncode} stderr={proc.stderr}")
        try:
            report = json.loads(proc.stdout)
            parsed = True
        except json.JSONDecodeError as exc:
            report, parsed = {}, False
            check("报告为合法 JSON", False, str(exc))
        check("报告为合法 JSON", parsed)
        check("顶层含 scan_root/risks/unchecked 三段",
              set(report.keys()) == {"scan_root", "risks", "unchecked"},
              str(report.keys()))
        check("scan_root 为绝对路径",
              report.get("scan_root") == os.path.abspath(TREE))

        risks, unchecked = report["risks"], report["unchecked"]

        print("\n== 验收 3：符号链接/联接点被跳过，普通文件仍完整覆盖 ==")
        link_paths = [r["file"] for r in risks + unchecked
                      if "link_to_sub" in r["file"]]
        check("报告任何清单都不含链接本身", link_paths == [], str(link_paths))
        # inside.txt 位于被链接指向的真实目录中，应通过真实路径被遍历到
        # （无风险也无异常时不会出现在两个清单中，故直接向遍历器求证）
        walked = {p for kind, *rest in
                  file_audit._iter_regular_files(os.path.abspath(TREE))
                  if kind == "file" for p in [rest[0]]}
        inside_abs = os.path.abspath(os.path.join(TREE, "target", "inside.txt"))
        check("联接点目标目录内真实文件仍可经真实路径核查",
              inside_abs in walked, f"链接类型={link_kind}")
        # 链接本身绝不在遍历结果中
        link_abs = os.path.abspath(os.path.join(TREE, "link_to_sub"))
        check("联接点/符号链接本身未被遍历",
              not any(os.path.commonpath([link_abs, p]) == link_abs
                      for p in walked))
        # 普通文件即使无风险也不会出现在任何清单
        check("普通文件 plain.txt 未被错误归入未能核查清单",
              not any(u["file"].endswith("plain.txt") for u in unchecked))

        print("\n== 验收 4：普通账号可写命中 write 风险 ==")
        w_hits = [r for r in risks
                  if r["file"].endswith("acct_writable.txt")
                  and r["access"] == "write"]
        check("acct_writable.txt 出现 write 风险记录", len(w_hits) == 1,
              str(w_hits))

        print("\n== 验收 5：同文件 read/write 双规则逐条列出、不合并 ==")
        multi = [r for r in risks if r["file"].endswith("multi_rw.txt")]
        users_records = [r for r in multi if r["identity"].endswith("Users")
                         or r["identity"].endswith("用户")]
        accesses = sorted(r["access"] for r in users_records)
        check("multi_rw.txt 对同一身份存在 read、write 两条独立记录",
              accesses == ["read", "write"], str(users_records))

        print("\n== 验收 6：风险记录三字段齐全且均为字符串、路径为绝对路径 ==")
        shape_ok = all(
            set(r.keys()) == {"file", "identity", "access"}
            and all(isinstance(r[k], str) and r[k] for k in
                    ("file", "identity", "access"))
            and os.path.isabs(r["file"])
            for r in risks
        )
        check("全部风险记录字段与类型合规", shape_ok, str(risks[:2]))

        print("\n== 验收 7/8：占锁文件进入未能核查清单并附原因，其余不受影响 ==")
        lock_rows = [u for u in unchecked
                     if u["file"].endswith("locked.txt")]
        check("locked.txt 出现在未能核查清单", len(lock_rows) == 1,
              str(unchecked))
        reason_ok = bool(lock_rows) and (
            "WinError 32" in lock_rows[0]["reason"]
            or "共享" in lock_rows[0]["reason"]
            or "锁定" in lock_rows[0]["reason"]
        )
        check("原因简述包含共享冲突信息", reason_ok,
              str(lock_rows))
        check("风险清单未受占锁文件影响（仍为 5 条）", len(risks) == 5,
              f"实际 {len(risks)} 条")
        check("未能核查清单条目含 file/reason 且为字符串",
              all(set(u.keys()) == {"file", "reason"}
                  and isinstance(u["file"], str)
                  and isinstance(u["reason"], str) for u in unchecked))

        print("\n== 验收 9：file → identity → access 两层升序 ==")
        keys = [(r["file"], r["identity"], r["access"]) for r in risks]
        check("排序与内置排序结果一致", keys == sorted(keys), str(keys))
        # 多身份同文件的 identity 层排序样本
        multi_identities = [r["identity"] for r in
                            [x for x in risks
                             if x["file"].endswith("multi_rw.txt")]]
        check("multi_rw.txt 内 identity 层有序",
              multi_identities == sorted(multi_identities),
              str(multi_identities))

        print("\n== 验收 10：不存在身份不产生记录，其余规则照常命中 ==")
        proc_ghost = run_tool(TREE, rule_path("rules_unknown_sid.json"))
        check("含幽灵 SID 的规则文件仍正常退出 0",
              proc_ghost.returncode == 0, proc_ghost.stderr)
        ghost_report = json.loads(proc_ghost.stdout)
        check("幽灵身份不产生任何风险记录",
              all("9999" not in r["identity"] for r in
                  ghost_report["risks"]))
        check("同文件中的有效规则仍命中（acct_writable write 保留）",
              any(r["file"].endswith("acct_writable.txt")
                  and r["access"] == "write"
                  for r in ghost_report["risks"]))
        check("占锁文件在幽灵规则运行中同样进入未能核查",
              any(u["file"].endswith("locked.txt")
                  for u in ghost_report["unchecked"]))

        print("\n== 验收 2/11：非法规则整体拒绝、非零退出、无报告输出 ==")
        for bad in ("rules_bad_access.json", "rules_bad_shape.json",
                    "rules_not_array.json", "rules_broken.json"):
            bad_proc = run_tool(TREE, rule_path(bad))
            check(f"{bad}: 非零退出且标准输出为空",
                  bad_proc.returncode != 0 and bad_proc.stdout.strip() == "",
                  f"rc={bad_proc.returncode}")
            # stderr 为非空错误文本（控制台渲染可能按 OEM 代码页显示乱码，
            # 这里只验证存在错误输出，具体文案见手工样例）
            check(f"{bad}: 错误信息输出到标准错误",
                  bool(bad_proc.stderr.strip()), bad_proc.stderr.strip())

        print("\n== 验收 1：目录不存在 -> 明确提示且非零退出 ==")
        missing = os.path.join(WORKSPACE, "no_such_dir")
        miss_proc = run_tool(missing, rule_path("rules_valid.json"))
        check("不存在目录非零退出", miss_proc.returncode != 0)
        check("提示目录不可用且无报告输出（stderr 非空）",
              bool(miss_proc.stderr.strip())
              and miss_proc.stdout.strip() == "",
              miss_proc.stderr.strip())

        print("\n== 补充：空规则与重复规则语义 ==")
        empty_proc = run_tool(TREE, rule_path("rules_empty.json"))
        empty_report = json.loads(empty_proc.stdout)
        check("空规则：退出 0、零风险、未能核查仍保留",
              empty_proc.returncode == 0
              and empty_report["risks"] == []
              and len(empty_report["unchecked"]) == 1)
        dup_proc = run_tool(TREE, rule_path("rules_dup.json"))
        dup_report = json.loads(dup_proc.stdout)
        dup_w = [r for r in dup_report["risks"]
                 if r["file"].endswith("acct_writable.txt")]
        check("重复规则逐条记录（acct_writable 出现 2 条 write）",
              len(dup_w) == 2, str(dup_w))

        print("\n== 实际报告样例（rules_valid.json） ==")
        print(json.dumps(report, ensure_ascii=False, indent=2))

    finally:
        locker.terminate()
        try:
            locker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            locker.kill()

    print(f"\n==== 汇总: PASS={PASS_COUNT} FAIL={FAIL_COUNT} ====")
    return 0 if FAIL_COUNT == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
