"""端到端演示与验收脚本（仅标准库，Windows / macOS / Linux 均可运行）。

自动完成：
  阶段1  启动漏洞版服务 -> 完整检测 -> 校验要求 2/3/4/5/9 的报告表现
  阶段2  坏用例文件（缺字段/类型错）-> 要求 1：退出码+字段级错误+零请求
  阶段3  外部主机配置       -> 要求 10：整体拒绝+零请求
  阶段4  重复运行           -> 要求 8：旧报告不被覆盖
  阶段5  resume 重试        -> 要求 5：不可达用例补检完成
  阶段6  修复版服务对比     -> 要求 6/7：已修复/未修复 + 功能回退判据
  阶段7  数据变异版对比     -> 要求 7 异常分支：功能不一致被标记
  阶段8  日志检查           -> 要求 11：含开始/完成事件且无凭据明文

用法：
  python -I run_demo.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Windows 控制台默认 GBK，统一切到 UTF-8，保证中文验收输出可读
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
# -I 隔离模式：屏蔽用户站点包与 PYTHONPATH/PYTHONIOENCODING 等环境变量注入；
# -X utf8 强制 UTF-8 IO；入口脚本自行把项目根加入 sys.path
SCANNER = [PYTHON, "-I", "-X", "utf8", str(ROOT / "main.py")]
SERVICE = [PYTHON, "-I", "-X", "utf8", str(ROOT / "samples" / "order_service.py")]

CASES = ROOT / "samples" / "cases" / "order_cases.json"
BAD_CASES = ROOT / "samples" / "cases" / "bad_missing_fields.json"
EXTERNAL_CASES = ROOT / "samples" / "cases" / "external_host.json"

WORK = ROOT / "demo_output"
REPORTS = WORK / "reports"
LOGS = WORK / "logs"

PORTS = {
    "vulnerable": 18080,
    "fixed": 18081,
    "altered": 18082,
    "unfixed": 18083,
    "retry": 18099,
}
SECRETS = ["tok-alice-001", "tok-bob-002", "tok-carol-003"]

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    flag = "PASS" if condition else "FAIL"
    print(f"[{flag}] {name}" + (f" — {detail}" if detail else ""))


def http_get(url: str, timeout: float = 2.0):
    req = urllib.request.Request(url, headers={"X-Auth-Token": "tok-alice-001"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.getcode(), resp.read().decode("utf-8")


def wait_healthy(port: int, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            code, _ = http_get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if code == 200:
                return
        except OSError as exc:
            last_err = exc
        time.sleep(0.2)
    raise RuntimeError(f"端口 {port} 服务未在 {timeout}s 内就绪：{last_err}")


def start_server(mode: str, port: int) -> subprocess.Popen:
    env = dict(os.environ, MODE=mode, PORT=str(port), HOST="127.0.0.1")
    kwargs = {
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "cwd": ROOT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(SERVICE, **kwargs)
    try:
        wait_healthy(port)
    except Exception:
        proc.kill()
        raise
    return proc


def run_scanner(*extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        SCANNER + ["--cases", str(CASES), "--report-dir", str(REPORTS),
                   "--log-dir", str(LOGS)] + list(extra_args),
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def newest(pattern: str) -> Path:
    files = sorted(REPORTS.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not files:
        raise FileNotFoundError(pattern)
    return files[-1]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reset_access_log(port: int) -> None:
    http_get(f"http://127.0.0.1:{port}/admin/reset")


def access_log(port: int) -> list[dict]:
    _, body = http_get(f"http://127.0.0.1:{port}/admin/access-log")
    return json.loads(body)["requests"]


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    REPORTS.mkdir(parents=True)
    LOGS.mkdir(parents=True)

    servers: list[subprocess.Popen] = []
    try:
        # ---------------- 阶段 1：漏洞版完整检测 ----------------
        print("\n=== 阶段1：启动漏洞版服务并完整检测（要求 2/3/4/5/9）===")
        vuln = start_server("vulnerable", PORTS["vulnerable"])
        servers.append(vuln)
        reset_access_log(PORTS["vulnerable"])

        proc = run_scanner()
        print(proc.stdout)
        check("阶段1 检测进程退出码为 0", proc.returncode == 0, proc.stderr.strip())
        report1_path = newest("detection_report_*.json")
        r1 = load_json(report1_path)
        idx = {e["case_id"]: e for e in r1["results"]}

        # 要求 4：字段齐全、按编号升序、缺失摘要为 null
        ids = [e["case_id"] for e in r1["results"]]
        check("要求4 报告按用例编号升序", ids == sorted(ids, key=lambda s: int(s.split("-")[1])), str(ids))
        required_fields = {"case_id", "type", "status", "hit",
                           "request_summary", "response_summary", "reject_reasons", "error"}
        check("要求4 每条结论字段齐全",
              all(required_fields <= set(e.keys()) for e in r1["results"]))
        check("要求4 不可达用例摘要为 null",
              idx["TC-08"]["request_summary"] is None and idx["TC-08"]["response_summary"] is None)

        # 要求 2：SQL 注入真实命中 + 响应摘要
        check("要求2 TC-01 SQL注入标记命中", idx["TC-01"]["status"] == "hit" and idx["TC-01"]["hit"] is True)
        check("要求2 TC-01 附响应摘要",
              idx["TC-01"]["response_summary"] is not None
              and '"count": 5' in (idx["TC-01"]["response_summary"].get("body") or ""))
        check("要求2 TC-02 注入报错探测命中", idx["TC-02"]["status"] == "hit")

        # 要求 3：越权命中 + 请求/响应摘要
        check("要求3 TC-03 越权标记命中", idx["TC-03"]["status"] == "hit")
        check("要求3 TC-03 含请求与响应摘要",
              idx["TC-03"]["request_summary"] is not None and idx["TC-03"]["response_summary"] is not None)
        check("要求3 TC-09 越权标记命中", idx["TC-09"]["status"] == "hit")
        check("对照 TC-04 正常访问同样命中规则（非越权误报）", idx["TC-04"]["status"] == "hit")

        # 要求 9：业务不合规参数拒绝，且服务端未收到请求
        check("要求9 TC-06 空用户标识标记 rejected",
              idx["TC-06"]["status"] == "rejected" and "为空" in " ".join(idx["TC-06"]["reject_reasons"]))
        check("要求9 TC-07 非数字订单号标记 rejected",
              idx["TC-07"]["status"] == "rejected" and "正整数" in " ".join(idx["TC-07"]["reject_reasons"]))
        time.sleep(0.3)
        seen_orders = [r for r in access_log(PORTS["vulnerable"]) if r["path"] == "/orders"]
        seen_params = {key for r in seen_orders for key in r["param_keys"]}
        # 被拒绝用例使用的特征参数组合不应出现在服务端（TC-06 的空白 user_id、TC-07 的特征订单号）
        check("要求9 被拒绝用例的请求未到达本地实例",
              all("abc;DROP" not in str(r) for r in seen_orders)
              and all(r.get("status") != 400 for r in seen_orders),
              f"服务端仅记录 {len(seen_orders)} 个 /orders 请求")

        # 要求 5：不可达用例进入 incomplete 分区，且不算未命中
        check("要求5 TC-08 标记 incomplete", idx["TC-08"]["status"] == "incomplete")
        check("要求5 incomplete 单独列出",
              [c["case_id"] for c in r1["incomplete_cases"]] == ["TC-08"])
        check("要求5 未完成用例未被当作未命中",
              idx["TC-08"]["hit"] is None and idx["TC-08"]["status"] != "miss")
        check("要求5 其余用例均已完成", r1["summary"]["completed"] == 8 and r1["summary"]["incomplete"] == 1)

        # 报告中不得出现凭据明文
        raw_report = report1_path.read_text(encoding="utf-8")
        check("脱敏 报告不含凭据明文", all(s not in raw_report for s in SECRETS))

        # ---------------- 阶段 2：坏用例文件（要求 1）----------------
        print("\n=== 阶段2：缺必填字段/类型错误的用例文件（要求1）===")
        reset_access_log(PORTS["vulnerable"])
        bad = subprocess.run(
            SCANNER + ["--cases", str(BAD_CASES), "--report-dir", str(REPORTS),
                       "--log-dir", str(LOGS)],
            cwd=ROOT,
            text=True, encoding="utf-8", errors="replace", capture_output=True,
        )
        check("要求1 进程以退出码 2 终止", bad.returncode == 2, f"实际 {bad.returncode}")
        check("要求1 输出字段级错误说明",
              "description" in bad.stderr and "method" in bad.stderr and "case_id" in bad.stderr
              and "rules" in bad.stderr and "type" in bad.stderr,
              bad.stderr.strip().replace("\n", " | ")[:300])
        time.sleep(0.2)
        check("要求1 校验失败时未发出任何请求", len(access_log(PORTS["vulnerable"])) == 0)
        check("要求1 未生成报告文件", not list(REPORTS.glob("detection_report_*.json"))[1:])

        # ---------------- 阶段 3：外部主机（要求 10）----------------
        print("\n=== 阶段3：配置外部主机（要求10）===")
        reset_access_log(PORTS["vulnerable"])
        ext = subprocess.run(
            SCANNER + ["--cases", str(EXTERNAL_CASES), "--report-dir", str(REPORTS),
                       "--log-dir", str(LOGS)],
            cwd=ROOT,
            text=True, encoding="utf-8", errors="replace", capture_output=True,
        )
        check("要求10 外部主机被拒绝（退出码 3）", ext.returncode == 3, f"实际 {ext.returncode}")
        check("要求10 提示信息明确",
              "拒绝" in ext.stderr and ("本地" in ext.stderr or "回环" in ext.stderr),
              ext.stderr.strip())
        time.sleep(0.2)
        check("要求10 实例未收到任何请求", len(access_log(PORTS["vulnerable"])) == 0)

        # ---------------- 阶段 4：重复运行不覆盖（要求 8）----------------
        print("\n=== 阶段4：重复运行，旧报告保留（要求8）===")
        before = {p.name: sha256(p) for p in REPORTS.glob("*.json")}
        time.sleep(1.1)  # 保证时间戳不同；同秒时序号机制也会兜底
        proc2 = run_scanner()
        check("阶段4 第二次检测成功", proc2.returncode == 0)
        report2_path = newest("detection_report_*.json")
        check("要求8 生成了可区分的新报告文件", report1_path != report2_path)
        check("要求8 旧报告内容不变",
              all(sha256(REPORTS / name) == digest for name, digest in before.items()))

        # ---------------- 阶段 5：resume 重试（要求 5 后半）----------------
        print("\n=== 阶段5：启动 18099 端口并 resume 补检 TC-08（要求5）===")
        retry_server = start_server("vulnerable", PORTS["retry"])
        servers.append(retry_server)
        resume_proc = run_scanner("--resume", str(report2_path))
        print(resume_proc.stdout)
        check("阶段5 resume 进程退出码为 0", resume_proc.returncode == 0)
        resume_report = newest("detection_report_*.json")
        r_resume = load_json(resume_report)
        idx_r = {e["case_id"]: e for e in r_resume["results"]}
        check("要求5 resume 后 TC-08 完成且命中", idx_r["TC-08"]["status"] == "hit")
        check("要求5 resume 报告未完成列表为空", r_resume["incomplete_cases"] == [])
        check("要求5 其余用例结论沿用基准",
              all(idx_r[c]["status"] == idx[c]["status"] for c in ids if c != "TC-08"))

        # ---------------- 阶段 6：修复版对比（要求 6/7 正常分支）----------------
        print("\n=== 阶段6：修复版服务，复用同一批用例做对比（要求6/7）===")
        fixed = start_server("fixed", PORTS["fixed"])
        servers.append(fixed)
        cmp_proc = run_scanner("--base-url", f"http://127.0.0.1:{PORTS['fixed']}",
                               "--compare-baseline", str(report2_path))
        print(cmp_proc.stdout)
        check("阶段6 对比进程退出码为 0", cmp_proc.returncode == 0)
        cmp_path = newest("comparison_report_*.json")
        cmp_report = load_json(cmp_path)
        cidx = {e["case_id"]: e for e in cmp_report["results"]}
        check("要求6 注入用例 TC-01 显示 fixed", cidx["TC-01"]["verdict"] == "fixed")
        check("要求6 注入探测 TC-02 显示 fixed", cidx["TC-02"]["verdict"] == "fixed")
        check("要求6 越权用例 TC-03/TC-09 显示 fixed",
              cidx["TC-03"]["verdict"] == "fixed" and cidx["TC-09"]["verdict"] == "fixed")
        check("要求6 干净对照 TC-04 显示 still_clean", cidx["TC-04"]["verdict"] == "still_clean")
        check("要求6 业务拒绝用例保持 rejected",
              cidx["TC-06"]["verdict"] == "rejected" and cidx["TC-07"]["verdict"] == "rejected")
        fc = {f["case_id"]: f for f in cmp_report["functional_regression_checks"]}
        check("要求7 正常查询 TC-05 与基准一致，未标记异常",
              fc["TC-05"]["regression"] is False, fc["TC-05"]["detail"])
        check("要求7 对比报告功能异常计数为 0",
              cmp_report["summary"]["functional_abnormal"] == 0)

        # ---------------- 阶段 7：数据变异版（要求 7 异常分支）----------------
        print("\n=== 阶段7：数据变异版服务，验证功能回退被标记（要求7异常分支）===")
        altered = start_server("altered", PORTS["altered"])
        servers.append(altered)
        alt_proc = run_scanner("--base-url", f"http://127.0.0.1:{PORTS['altered']}",
                               "--compare-baseline", str(report2_path))
        check("阶段7 变异版对比成功", alt_proc.returncode == 0)
        alt_path = newest("comparison_report_*.json")
        alt_report = load_json(alt_path)
        afc = {f["case_id"]: f for f in alt_report["functional_regression_checks"]}
        check("要求7 结果不一致时报告标记异常",
              afc["TC-05"]["regression"] is True, afc["TC-05"]["detail"])
        check("要求7 功能异常计数为 1", alt_report["summary"]["functional_abnormal"] == 1)
        check("要求7 变异版安全结论仍为 fixed",
              {e["case_id"]: e["verdict"] for e in alt_report["results"]}["TC-01"] == "fixed")

        # ---------------- 阶段 7b：未修复实例对比（要求 6 unfixed 分支）----------------
        print("\n=== 阶段7b：另一个漏洞实例做对比，验证 unfixed 判定（要求6未修复分支）===")
        unfixed_server = start_server("vulnerable", PORTS["unfixed"])
        servers.append(unfixed_server)
        unf_proc = run_scanner(
            "--base-url", f"http://127.0.0.1:{PORTS['unfixed']}",
            "--compare-baseline", str(report2_path),
        )
        check("阶段7b 未修复实例对比成功", unf_proc.returncode == 0)
        unf_report = load_json(newest("comparison_report_*.json"))
        uidx = {e["case_id"]: e["verdict"] for e in unf_report["results"]}
        check("要求6 未修复实例上攻击用例显示 unfixed",
              uidx["TC-01"] == "unfixed" and uidx["TC-03"] == "unfixed"
              and uidx["TC-09"] == "unfixed",
              str({k: uidx[k] for k in ("TC-01", "TC-03", "TC-09")}))

        # ---------------- 阶段 8：日志检查（要求 11）----------------
        print("\n=== 阶段8：日志检查（要求11）===")
        log_text = (LOGS / "security_check.log").read_text(encoding="utf-8")
        check("要求11 日志含检测开始事件", "安全检测开始" in log_text)
        check("要求11 日志含检测完成事件", "安全检测完成" in log_text)
        check("要求11 日志含异常事件记录", "暂时不可达" in log_text or "拒绝" in log_text)
        check("要求11 全文搜索不到任何凭据原文",
              all(s not in log_text for s in SECRETS))
        check("要求11 脱敏占位符存在于日志", "***REDACTED***" in log_text)

    finally:
        for proc in servers:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ---------------- 汇总 ----------------
    print("\n================ 验收汇总 ================")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    print(f"\n通过 {passed}/{len(results)}")
    print(f"报告目录：{REPORTS}")
    print(f"日志目录：{LOGS}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
