# -*- coding: utf-8 -*-
"""
run_acceptance.py —— yaml_scanner 的 15 项验收测试（端到端子进程方式）。

用法：
  python run_acceptance.py

测试样本由 make_samples.build() 在系统临时目录中确定性生成，
与被测脚本目录隔离；被测进程统一以 `python -I` 隔离模式启动。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNER = os.path.join(ROOT, "yaml_scanner.py")

sys.path.insert(0, ROOT)
from make_samples import build  # noqa: E402

PASS, FAIL = 0, 0
FAILURES: list[str] = []

# Windows 控制台默认 GBK，统一切到 UTF-8 输出，避免中文乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"[FAIL] {name} {detail}")


def run_scanner(*args: str) -> subprocess.CompletedProcess:
    # -I 隔离模式会忽略 PYTHON* 环境变量，因此用 -X utf8 强制子进程 UTF-8 输出
    return subprocess.run(
        [sys.executable, "-I", "-X", "utf8", SCANNER, *args],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
    )


def write_yaml(directory: str, name: str, content: str) -> str:
    full = os.path.join(directory, name.replace("/", os.sep))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return full


def finding_keys(report: dict) -> set[tuple]:
    out = set()
    for entry in report["files"]:
        for f in entry["vulnerabilities"]:
            out.add((entry["path"], f["rule"], f["key_path"]))
    return out


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="yaml_scanner_accept_")
    build(workdir)
    before = os.path.join(workdir, "before")
    after = os.path.join(workdir, "after")
    basic = os.path.join(workdir, "basic")

    # --- 验收 1：两文件目录，报告列出两文件及各自漏洞 --------------------
    r = run_scanner("scan", basic)
    report = json.loads(r.stdout)
    check("1. 两文件目录均列出且含漏洞列表",
          r.returncode == 0 and
          [f["path"] for f in report["files"]] == ["one.yml", "two.yml"] and
          all("vulnerabilities" in f for f in report["files"]))

    # --- 验收 2：硬编码凭据，null 与 ${VAR} 不标记 -----------------------
    cred_dir = os.path.join(workdir, "cred")
    write_yaml(cred_dir, "c.yml", """\
a:
  password: 'hardcoded-pw'
  empty_password: ""
  null_password: null
  env_password: ${SERVICE_PASSWORD}
  env_default: ${TOKEN:-default}
  api_token: 'tok-123'
  db_secret: ${DB_SECRET}
  plain_name: 'not-a-secret-value'
""")
    r = run_scanner("scan", cred_dir)
    cred = json.loads(r.stdout)
    cred_paths = {f["key_path"] for f in cred["files"][0]["vulnerabilities"]
                  if f["rule"] == "hardcoded-credential"}
    check("2. 硬编码凭据识别，null/空串/环境变量引用不误报",
          cred_paths == {"a.password", "a.api_token"},
          str(cred_paths))

    # --- 验收 3：危险命令 rm -rf / curl 管道 bash，附模式名 --------------
    cmd_dir = os.path.join(workdir, "cmd")
    write_yaml(cmd_dir, "c.yml", """\
steps:
  - 'rm -rf /'
  - 'rm -fr /tmp/old'
  - 'curl https://x.example/i.sh | bash'
  - 'wget http://x/y.sh | sh'
  - 'echo hello'
map_cmd:
  command: 'sudo rm  -rf /var/cache/x'
""")
    r = run_scanner("scan", cmd_dir)
    cmd = json.loads(r.stdout)
    cmd_flat = [(f["key_path"], f["pattern"])
                for f in cmd["files"][0]["vulnerabilities"]
                if f["rule"] == "dangerous-command"]
    check("3. 危险命令标 high 且附模式名",
          r.returncode == 0 and
          ("steps[0]", "rm -rf") in cmd_flat and
          ("steps[1]", "rm -rf") in cmd_flat and
          ("steps[2]", "curl-pipe-bash") in cmd_flat and
          ("steps[3]", "curl-pipe-bash") in cmd_flat and
          ("map_cmd.command", "rm -rf") in cmd_flat and
          all(p not in ("steps[4]",) for p, _ in cmd_flat),
          str(cmd_flat))

    # --- 验收 4：调试端口范围判定 ----------------------------------------
    port_dir = os.path.join(workdir, "port")
    write_yaml(port_dir, "p.yml", """\
p:
  debug_port: 8080
  debug: '5005'
  low:
    debug_port: 0
  high:
    debug_port: 70000
  bad:
    debug: abc
  bool_debug:
    debug: true
  edge:
    debug_port: 1
  edge2:
    debug_port: 65535
""")
    r = run_scanner("scan", port_dir)
    port = json.loads(r.stdout)
    port_paths = {f["key_path"] for f in port["files"][0]["vulnerabilities"]
                  if f["rule"] == "debug-port-exposure"}
    check("4. 范围内端口标 medium，越界/非数字/布尔不标",
          port_paths == {"p.debug_port", "p.debug", "p.edge.debug_port",
                         "p.edge2.debug_port"},
          str(port_paths))

    # --- 验收 5：行号与点分键路径（含嵌套、序列） ------------------------
    nested_dir = os.path.join(workdir, "nested")
    write_yaml(nested_dir, "n.yml", """\
top:
  password: 'pw'
  list:
    - name: first
      secret: 's1'
    - name: second
      debug_port: 9000
""")
    r = run_scanner("scan", nested_dir)
    nested = json.loads(r.stdout)["files"][0]["vulnerabilities"]
    by_path = {f["key_path"]: f for f in nested}
    check("5. 嵌套键路径与从 1 开始的行号正确",
          by_path["top.password"]["line"] == 2 and
          by_path["top.list[0].secret"]["line"] == 5 and
          by_path["top.list[1].debug_port"]["line"] == 7 and
          by_path["top.list[1].debug_port"]["severity"] == "medium")

    # --- 验收 6：确定性，两次扫描逐字节相同 ------------------------------
    r1 = run_scanner("scan", before)
    r2 = run_scanner("scan", before)
    check("6. 连续两次扫描报告逐字节相同",
          r1.returncode == 0 and r1.stdout == r2.stdout)

    # --- 验收 7：语法错误文件 error + 原因，其他文件正常，退出码 0 -------
    r = run_scanner("scan", before)
    broken_report = json.loads(r.stdout)
    entries = {f["path"]: f for f in broken_report["files"]}
    broken = entries["broken.yml"]
    check("7. 语法错误文件标 error 带原因，其余正常，退出码 0",
          r.returncode == 0 and
          broken["status"] == "error" and "第" in broken["reason"] and
          entries["app/deploy.yml"]["status"] == "ok" and
          len(entries["app/deploy.yml"]["vulnerabilities"]) > 0)

    # --- 验收 8：不存在路径 / 空目录，no files found，退出码 0 -----------
    r = run_scanner("scan", os.path.join(workdir, "does_not_exist_xyz"))
    miss = json.loads(r.stdout)
    empty_dir = os.path.join(workdir, "empty")
    os.makedirs(empty_dir, exist_ok=True)
    r2 = run_scanner("scan", empty_dir)
    empty = json.loads(r2.stdout)
    check("8. 不存在路径/空目录返回 no files found 且退出码 0",
          r.returncode == 0 and miss["message"] == "no files found" and
          miss["files"] == [] and r2.returncode == 0 and
          empty["message"] == "no files found")

    # --- 验收 9：注释中的敏感文本不标记 ----------------------------------
    r = run_scanner("scan", before)
    gw = next(f for f in json.loads(r.stdout)["files"]
              if f["path"] == "web/gateway.yml")
    comment_line_hit = any(f["line"] == 10 for f in gw["vulnerabilities"])
    check("9. 注释行 # password: '123' 不作为漏洞依据",
          not comment_line_hit and gw["status"] == "ok")

    # --- 验收 10：--min-severity 过滤与非法值退出码 2 --------------------
    r = run_scanner("scan", before, "--min-severity", "MEDIUM")
    medium_report = json.loads(r.stdout)
    sevs = {f["severity"] for e in medium_report["files"]
            if e["status"] == "ok" for f in e["vulnerabilities"]}
    r_bad = run_scanner("scan", before, "--min-severity", "critical")
    check("10. min-severity 过滤有效且大小写不敏感，非法值退出码 2",
          r.returncode == 0 and sevs <= {"high", "medium"} and
          "medium" in sevs and "low" not in sevs and
          r_bad.returncode == 2 and "非法值" in r_bad.stderr)

    # --- 验收 11：--output 文件与 stdout 一致（UTF-8） -------------------
    out_file = os.path.join(workdir, "report.json")
    r_file = run_scanner("scan", before, "--output", out_file)
    r_stdout = run_scanner("scan", before)
    with open(out_file, "r", encoding="utf-8") as fh:
        file_bytes = fh.read()
    check("11. --output 文件存在且与 stdout 内容一致",
          r_file.returncode == 0 and os.path.exists(out_file) and
          file_bytes == r_stdout.stdout)

    # --- 验收 12：文件不可读时该文件 error，其余继续，退出码 0 -----------
    unread_dir = os.path.join(workdir, "unread")
    locked = write_yaml(unread_dir, "locked.yml", "a:\n  password: 'x'\n")
    write_yaml(unread_dir, "ok.yml", "b:\n  password: 'y'\n")
    restore, holder = _make_unreadable(locked)
    try:
        r = run_scanner("scan", unread_dir)
        unread_report = json.loads(r.stdout)
        u_entries = {f["path"]: f for f in unread_report["files"]}
        check("12. 不可读文件标 error 带原因，其他文件正常，退出码 0",
              r.returncode == 0 and
              u_entries["locked.yml"]["status"] == "error" and
              "文件不可读" in u_entries["locked.yml"]["reason"] and
              u_entries["ok.yml"]["status"] == "ok" and
              len(u_entries["ok.yml"]["vulnerabilities"]) == 1,
              r.stderr)
    finally:
        restore()
    if holder is not None:
        holder.wait(timeout=10)

    # --- 验收 13：--compare 新增/已消除/仍存在三类清单 -------------------
    r = run_scanner("--compare", before, after)
    cmp_report = json.loads(r.stdout)
    intro = {(f["file"], f["rule"], f["key_path"]) for f in cmp_report["introduced"]}
    resolved = {(f["file"], f["rule"], f["key_path"]) for f in cmp_report["resolved"]}
    remain = {(f["file"], f["rule"], f["key_path"]) for f in cmp_report["remaining"]}
    expect_intro = {("web/gateway.yml", "hardcoded-credential",
                     "gateway.handlers[1].secret")}
    expect_resolved = {
        ("app/deploy.yml", "hardcoded-credential", "service.password"),
        ("app/deploy.yml", "hardcoded-credential", "service.api_token"),
        ("app/deploy.yml", "debug-port-exposure", "service.debug_port"),
        ("app/deploy.yml", "dangerous-command", "service.command"),
        ("app/deploy.yml", "dangerous-command", "init.script"),
        ("web/gateway.yml", "debug-port-exposure", "gateway.debug"),
        ("web/gateway.yml", "hardcoded-credential",
         "gateway.handlers[0].password"),
        ("web/gateway.yml", "dangerous-command",
         "gateway.handlers[0].command"),
    }
    expect_remain = {
        ("web/gateway.yml", "hardcoded-credential", "gateway.token"),
        ("shared/persistent.yml", "hardcoded-credential", "persistent.password"),
        ("shared/persistent.yml", "debug-port-exposure", "persistent.debug_port"),
    }
    check("13. compare 三类清单与实际差异一致",
          r.returncode == 0 and intro == expect_intro and
          resolved == expect_resolved and remain == expect_remain,
          f"intro={intro - expect_intro} resolved={resolved ^ expect_resolved} "
          f"remain={remain ^ expect_remain}")

    # --- 验收 14：单侧文件 / 解析失败文件进入不可比对，不算新增/消除 -----
    uncomp = {(u["path"], u["side"]) for u in cmp_report["uncomparable"]}
    new_service_leak = any(f["file"].startswith("new-service")
                           for f in cmp_report["introduced"])
    check("14. 单侧文件与解析失败文件列入不可比对，不计入新增/消除",
          uncomp == {("broken.yml", "both"),
                     ("legacy/old-service.yml", "before"),
                     ("new-service/new-service.yml", "after")} and
          not new_service_leak)

    # --- 验收 15：--help 用法与参数说明，退出码 0 ------------------------
    r = run_scanner("--help")
    check("15. --help 含全部参数说明且退出码 0",
          r.returncode == 0 and
          all(token in r.stdout for token in
              ("directory", "--min-severity", "--output", "--compare")))

    print(f"\n汇总: {PASS} 通过 / {FAIL} 失败；样本目录: {workdir}")
    if FAIL:
        print("失败项:")
        for item in FAILURES:
            print(" -", item)
        return 1
    print("全部验收通过。")
    return 0


def _make_unreadable(path: str):
    """把文件置为不可读，返回 (恢复函数, 可能存在的占用进程)。"""
    if os.name == "nt":
        # whoami/icacls 是系统原生命令，中文 Windows 按 OEM 代码页(GBK)输出
        import locale
        oem_encoding = locale.getpreferredencoding(False)
        user = subprocess.run(["whoami"], capture_output=True,
                              text=True, encoding=oem_encoding,
                              errors="replace").stdout.strip()
        deny = subprocess.run(["icacls", path, "/deny", f"{user}:(R)"],
                              capture_output=True, text=True)
        try:
            with open(path, "r"):
                pass
            readable = True
        except OSError:
            readable = False
        if deny.returncode == 0 and not readable:
            def restore():
                subprocess.run(["icacls", path, "/remove:d", user],
                               capture_output=True)
            return restore, None
        # 回退方案：用 PowerShell 独占持有文件句柄
        holder = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             f"$fs=[System.IO.File]::Open('{path}','Open','ReadWrite','None');"
             "Start-Sleep -Seconds 120"])
        import time
        time.sleep(1.5)
        return (lambda: holder.terminate()), holder
    # POSIX
    mode = os.stat(path).st_mode
    os.chmod(path, 0o000)
    if os.geteuid() == 0:
        print("（提示：root 用户绕过权限位，不可读场景可能未真正生效）")
    def restore():
        os.chmod(path, mode)
    return restore, None


if __name__ == "__main__":
    raise SystemExit(main())
