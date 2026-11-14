#!/usr/bin/env python3
"""项目47 一键本地验证脚本（仅标准库，Windows/Linux 均可运行）。

自动完成：
  A. 参数/地址/配置类前置校验（不发起请求、非零退出）
  B. 放行模式网关：三类载荷全部被接受
  C. 加固模式网关：三类载荷全部被拒绝
  D. 故障注入：网关对指定载荷断连，其余载荷结论保留、故障项原因非空
  E. JSON/证据/严重程度排序/摘要统计行校验
  F. 日志存在且 -v 后条目增多

用法：python run_demo.py
所有产物输出到 demo_output/。
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# Windows GBK 控制台下保证中文输出不崩：把标准流切到 UTF-8，并对无法编码字符兜底
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "demo_output"
PY = sys.executable
HOST = "127.0.0.1"
# 动态申请空闲端口，避免与本机其他项目（如 project46 的 18080）冲突
with socket.socket() as _s:
    _s.bind((HOST, 0))
    PORT = _s.getsockname()[1]
URL = f"http://{HOST}:{PORT}/upload"
REQUIRED_CATEGORIES = {"double_extension", "mime_content_mismatch", "scripted_filename"}
SEV_RANK = {"high": 0, "medium": 1, "low": 2}

results_log: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results_log.append((name, bool(cond), detail))
    flag = "PASS" if cond else "FAIL"
    print(f"[{flag}] {name}" + (f" -- {detail}" if detail else ""))


def wait_port(host: str, port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.1)
    return False


class Gateway:
    def __init__(self, mode: str, drop_on: str = ""):
        self.mode, self.drop_on, self.proc = mode, drop_on, None

    def __enter__(self):
        cmd = [PY, "-I", str(ROOT / "gateway.py"), "--host", HOST,
               "--port", str(PORT), "--mode", self.mode,
               "--upload-path", "/upload", "--file-field", "file"]
        if self.drop_on:
            cmd += ["--drop-on", self.drop_on]
        self.proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        if not wait_port(HOST, PORT):
            out, err = self.proc.communicate(timeout=5)
            raise RuntimeError(f"网关启动失败\nstdout={out}\nstderr={err}")
        # 读取网关就绪横幅，确认监听者确实是本项目的 gateway.py，
        # 而不是占用了同端口的其他服务
        banner = ""
        deadline = time.time() + 3
        while time.time() < deadline and not banner:
            if self.proc.poll() is not None:
                raise RuntimeError("网关进程提前退出: "
                                   + (self.proc.stderr.read() or ""))
            banner = self.proc.stdout.readline().strip()
            time.sleep(0.05)
        expected = f"gateway listening on http://{HOST}:{PORT}/upload mode={self.mode}"
        if expected not in banner:
            self.__exit__()
            raise RuntimeError(f"网关就绪横幅不匹配，期望包含 {expected!r}，实际 {banner!r}")
        return self

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        return False


def run_scanner(tag: str, url: str = URL, config: str = "config.json",
                verbose: bool = False, expect_rc: int | None = None) -> tuple[int, dict, str, str, str]:
    d = OUT / tag
    d.mkdir(parents=True, exist_ok=True)
    out_json, summary, log_file = d / "result.json", d / "summary.txt", d / "scan.log"
    cmd = [PY, "-I", str(ROOT / "upload_scanner.py"), url,
           "--config", str(ROOT / config),
           "--output", str(out_json), "--summary", str(summary),
           "--log-file", str(log_file)]
    if verbose:
        cmd.append("-v")
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if expect_rc is not None:
        check(f"[{tag}] 退出码={proc.returncode}（期望 {expect_rc}）",
              proc.returncode == expect_rc, proc.stderr.strip()[:160])
    data = {}
    if out_json.exists():
        with open(out_json, encoding="utf-8") as fh:
            data = json.load(fh)
    return proc.returncode, data, str(summary), str(log_file), proc.stderr


def validate_result_set(tag: str, data: dict, expected_verdicts: dict[str, str]) -> None:
    rows = data.get("results", [])
    check(f"[{tag}] 三类载荷编号齐全",
          {r["category"] for r in rows} == REQUIRED_CATEGORIES
          and {r["payload_id"] for r in rows} == {"P01", "P02", "P03"})
    for r in rows:
        check(f"[{tag}] {r['payload_id']} 结论={expected_verdicts[r['category']]}",
              r["verdict"] == expected_verdicts[r["category"]], r["basis"])
        for key in ("payload_id", "verdict", "basis"):
            check(f"[{tag}] {r['payload_id']} 含字段 {key}", key in r and r[key] is not None)


def main() -> int:
    # 每次运行清空历史产物，保证结果只反映本次验证
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(exist_ok=True)
    print("=" * 70)
    print("A. 前置校验：参数缺失 / 外部地址 / 非法配置（均不得发起请求，退出码非 0）")
    print("=" * 70)

    # A1 省略参数
    p = subprocess.run([PY, "-I", str(ROOT / "upload_scanner.py")],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    check("A1 省略参数退出码非 0", p.returncode != 0,
          (p.stderr or "").strip().splitlines()[-1][:120] if (p.stderr or "").strip() else "")

    # A2 外部地址（保留 TEST-NET 段，保证不会命中真实主机；校验在发请求前即失败）
    d = OUT / "cli_external"; d.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        [PY, "-I", str(ROOT / "upload_scanner.py"), "http://203.0.113.5/upload",
         "--config", str(ROOT / "config.json"),
         "--output", str(d / "r.json"), "--summary", str(d / "s.txt"),
         "--log-file", str(d / "l.log")],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    check("A2 外部地址退出码非 0", p.returncode != 0)
    check("A2 错误信息提示回环限制",
          "回环" in (p.stderr or "") or "loopback" in (p.stderr or "").lower(),
          (p.stderr or "").strip()[:160])
    check("A2 未发起请求（无结果文件）", not (d / "r.json").exists())

    # A3 配置文件缺失
    p = subprocess.run(
        [PY, "-I", str(ROOT / "upload_scanner.py"), URL,
         "--config", str(ROOT / "no_such_config.json"),
         "--output", str(d / "r2.json"), "--summary", str(d / "s2.txt"),
         "--log-file", str(d / "l2.log")],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    check("A3 缺失配置退出码非 0", p.returncode != 0 and "配置" in (p.stderr or ""),
          (p.stderr or "").strip()[:160])

    # A4 配置内容非法
    bad_cfg = OUT / "bad_config.json"
    bad_cfg.write_text('{"file_field": "", "text_fields": {}, "upload_path": "upload"}',
                       encoding="utf-8")
    p = subprocess.run(
        [PY, "-I", str(ROOT / "upload_scanner.py"), URL,
         "--config", str(bad_cfg),
         "--output", str(d / "r3.json"), "--summary", str(d / "s3.txt"),
         "--log-file", str(d / "l3.log")],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    check("A4 非法字段退出码非 0",
          p.returncode != 0 and "file_field" in (p.stderr or ""),
          (p.stderr or "").strip()[:160])

    print("=" * 70)
    print("B. 放行模式网关（加固前）：三类载荷全部被接受")
    print("=" * 70)
    with Gateway("permissive"):
        rc, data, summary, log_file, _ = run_scanner(
            "permissive", expect_rc=1)  # 发现绕过 -> 退出码 1
        expected = {c: "accepted" for c in REQUIRED_CATEGORIES}
        validate_result_set("permissive", data, expected)
        check("B accepted_total=3", data.get("accepted_total") == 3,
              f"accepted_total={data.get('accepted_total')}")
        for r in data["results"]:
            if r["verdict"] == "accepted":
                check(f"B {r['payload_id']} 证据字符串非空",
                      isinstance(r.get("evidence"), str) and len(r["evidence"]) > 0)
        # 严重程度排序：被接受项 high -> medium -> low
        accepted = [r for r in data["results"] if r["verdict"] == "accepted"]
        ranks = [SEV_RANK[r["severity"]] for r in accepted]
        check("B 被接受项按严重程度降序", ranks == sorted(ranks),
              ", ".join(f"{r['payload_id']}:{r['severity']}" for r in accepted))
        text = Path(summary).read_text(encoding="utf-8")
        check("B 摘要含 High/Medium/Low 统计行",
              f"High: 1, Medium: 1, Low: 1" in text)
        check("B 摘要含按严重程度降序的漏洞行",
              text.find("[HIGH]") < text.find("[MEDIUM]") < text.find("[LOW]"))
        check("B 日志文件存在且含投递记录",
              Path(log_file).exists() and "deliver P01" in Path(log_file).read_text(encoding="utf-8"))

    print("=" * 70)
    print("C. 加固模式网关（加固后）：同一配置重跑，三类全部被拒绝")
    print("=" * 70)
    with Gateway("hardened"):
        rc, data, summary, log_file, _ = run_scanner(
            "hardened", expect_rc=0)  # 无绕过 -> 退出码 0
        expected = {c: "rejected" for c in REQUIRED_CATEGORIES}
        validate_result_set("hardened", data, expected)
        check("C accepted_total=0 且无残留 accepted",
              data.get("accepted_total") == 0
              and all(r["verdict"] != "accepted" for r in data["results"]))
        check("C 被拒绝项 evidence 为 null",
              all(r["evidence"] is None for r in data["results"]
                  if r["verdict"] == "rejected"))
        text = Path(summary).read_text(encoding="utf-8")
        check("C 摘要统计行为 High: 0, Medium: 0, Low: 0",
              "High: 0, Medium: 0, Low: 0" in text)
        for r in data["results"]:
            check(f"C {r['payload_id']} 判定依据包含加固拒绝原因",
                  "hardened validation rejected" in r["basis"])

    print("=" * 70)
    print("D. 故障注入：网关对 P02 断连，其余载荷结论保留，故障项原因非空")
    print("=" * 70)
    with Gateway("permissive", drop_on="P02"):
        rc, data, summary, log_file, _ = run_scanner(
            "drop", expect_rc=3)  # 存在未完成 -> 退出码 3
        rows = {r["payload_id"]: r for r in data["results"]}
        check("D P02 标记 incomplete", rows["P02"]["verdict"] == "incomplete")
        check("D P02 原因非空",
              isinstance(rows["P02"].get("error"), str) and len(rows["P02"]["error"]) > 0,
              rows["P02"].get("error", ""))
        check("D P01/P03 仍有结论",
              rows["P01"]["verdict"] == "accepted" and rows["P03"]["verdict"] == "accepted")
        check("D 故障不污染结论（accepted_total=2）", data.get("accepted_total") == 2)
        check("D 日志记录故障",
              "failed" in Path(log_file).read_text(encoding="utf-8"))

    print("=" * 70)
    print("E. 日志详细程度：默认 vs -v，DEBUG 条目增多")
    print("=" * 70)
    with Gateway("hardened"):
        _, _, _, log_normal, _ = run_scanner("verbose_off")
        _, _, _, log_verbose, _ = run_scanner("verbose_on", verbose=True)
        n_normal = len(Path(log_normal).read_text(encoding="utf-8").splitlines())
        n_verbose = len(Path(log_verbose).read_text(encoding="utf-8").splitlines())
        check("E -v 后日志条目增多", n_verbose > n_normal,
              f"normal={n_normal} verbose={n_verbose}")
        check("E 详细日志含原始响应/请求头 DEBUG 记录",
              "DEBUG" in Path(log_verbose).read_text(encoding="utf-8"))

    print("=" * 70)
    total = len(results_log)
    failed = [n for n, ok, _ in results_log if not ok]
    print(f"\n总计 {total} 项检查，通过 {total - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项:")
        for n, ok, detail in results_log:
            if not ok:
                print(f"  - {n} {detail}")
        return 1
    print("全部验收点通过。产物目录: demo_output/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
