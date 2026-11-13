"""13 项验收要求的本地自动化演示编排（纯标准库）。

职责：
  1. 以子进程方式启动/停止靶场的未修复、已修复（含部分修复）形态；
  2. 构造清单校验失败、暂时性故障、持续不可用、混合判定、空清单、1000 条场景等样本；
  3. 调用 checker.py 执行判定，收集退出码与报告，逐条断言验收条件；
  4. 将完整结果写入 demo_output/demo_run.log 与 demo_output/results_summary.json。

运行：
  python -I run_demos.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "demo_output"
PYTHON = sys.executable

# Windows 中文控制台默认 GBK，统一切到 UTF-8 避免输出与日志文件乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
# 子进程继承 UTF-8 输出环境
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# 端口规划（均绑定回环地址，不对外暴露）
# 每个故障注入用例使用独立端口，杜绝进程清理时序导致的“新旧实例同端口”串台
PORT_VULN = 18081
PORT_FIXED = 18082
PORT_PARTIAL = 18083
PORT_ALWAYS = 18084   # D9 专用：fixed + flaky-always 注入
PORT_D6_FLAKY = 18085  # D6 专用：vulnerable + flaky-first
PORT_D8_ALWAYS = 18086  # D8 专用：fixed + flaky-always
PORT_SCALE_VULN = 18087  # D13 专用：1000 条场景的未修复实例
PORT_SCALE_FIXED = 18088  # D13 专用：1000 条场景的已修复实例
PORT_DEAD = 18099  # 全程不启动任何服务，用于“形态持续不可用”演示
ALL_USED_PORTS = [
    PORT_VULN, PORT_FIXED, PORT_PARTIAL, PORT_ALWAYS,
    PORT_D6_FLAKY, PORT_D8_ALWAYS, PORT_SCALE_VULN, PORT_SCALE_FIXED,
]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def log(message: str) -> None:
    print(message, flush=True)


def wait_healthy(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:  # noqa: S310
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@dataclass
class ServerProcess:
    name: str
    port: int
    mode: str
    process: subprocess.Popen | None = None
    extra_args: list[str] = field(default_factory=list)
    _log_handle: Any = None

    def start(self) -> None:
        # 直接以脚本路径启动：隔离模式(-I)下 -m 不将 cwd 加入 sys.path，
        # 而脚本方式会自动把 range/ 的父目录（项目根）置于 sys.path
        # 关键：stdout 重定向到日志文件而非 PIPE——否则服务访问日志写满
        # OS 管道缓冲区（约 4-8KB）后会整体阻塞，导致后续请求挂死
        log_path = OUTPUT_DIR / f"server_{self.name}_p{self.port}.log"
        self._log_handle = log_path.open("w", encoding="utf-8")
        args = [
            PYTHON, "-I", "-u", str(PROJECT_ROOT / "range" / "server.py"),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--mode", self.mode,
            *self.extra_args,
        ]
        self.process = subprocess.Popen(
            args,
            cwd=PROJECT_ROOT,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if not wait_healthy(self.port):
            output = ""
            try:
                log_path = OUTPUT_DIR / f"server_{self.name}_p{self.port}.log"
                if log_path.exists():
                    output = log_path.read_text(encoding="utf-8")[:2000]
            except Exception:
                pass
            raise RuntimeError(f"靶场 {self.name} 启动超时。进程输出:\n{output}")
        log(f"  靶场 {self.name} 已就绪: http://127.0.0.1:{self.port} (mode={self.mode})")

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None
        # 确认端口已真正释放，避免后续用例绑定到仍存活的旧实例
        deadline = time.time() + 3
        while time.time() < deadline and port_in_use(self.port):
            time.sleep(0.1)
        if port_in_use(self.port):
            raise RuntimeError(f"靶场 {self.name} 停止后端口 {self.port} 仍被占用")
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


def run_checker(
    manifest: Path,
    report: Path,
    vuln_port: int,
    fixed_port: int,
    retries: int = 2,
) -> subprocess.CompletedProcess[str]:
    args = [
        PYTHON, "-I", str(PROJECT_ROOT / "checker.py"),
        str(manifest),
        "--output", str(report),
        "--vuln-url", f"http://127.0.0.1:{vuln_port}",
        "--fixed-url", f"http://127.0.0.1:{fixed_port}",
        "--retries", str(retries),
        "--retry-delay", "0.2",
        "--timeout", "4",
    ]
    return subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def load_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# 验收用例
# --------------------------------------------------------------------------- #
@dataclass
class CaseResult:
    requirement: str
    title: str
    passed: bool
    detail: str
    exit_code: int | None = None


class DemoSuite:
    def __init__(self) -> None:
        self.results: list[CaseResult] = []
        self.servers: list[ServerProcess] = []

    def record(self, requirement: str, title: str, passed: bool, detail: str,
               exit_code: int | None = None) -> None:
        self.results.append(CaseResult(requirement, title, passed, detail, exit_code))
        mark = "PASS" if passed else "FAIL"
        log(f"[{mark}] {requirement} {title}")
        for line in detail.splitlines():
            log(f"       {line}")

    # ---- D1 清单校验：缺字段 / 类型不符 ----
    def case_d1_invalid_manifest(self) -> None:
        report = OUTPUT_DIR / "should_not_exist_d1.json"
        if report.exists():
            report.unlink()
        proc = run_checker(
            PROJECT_ROOT / "scenarios" / "invalid_manifest.json",
            report, PORT_VULN, PORT_FIXED,
        )
        stdout = proc.stdout
        checks = {
            "退出码非零": proc.returncode != 0,
            "报缺 actor": "actor" in stdout and "缺少字段" in stdout,
            "报 query 类型错误": "query" in stdout and ("必须是对象" in stdout or "类型" in stdout),
            "报缺 path": "path" in stdout,
            "报 query 值类型": "user_id" in stdout and "必须是字符串" in stdout,
            "未写报告": not report.exists(),
        }
        detail = (
            f"退出码={proc.returncode}（期望非零）\n"
            f"工具输出:\n{_indent(stdout.strip())}\n"
            f"断言: {_checks_str(checks)}"
        )
        self.record("验收1", "缺字段/类型不符时报错、非零退出且不执行场景",
                    all(checks.values()), detail, proc.returncode)

    # ---- D2 默认路径 + 自定义路径 ----
    def case_d2_paths(self, vuln: ServerProcess, fixed: ServerProcess) -> None:
        # (a) 无参数：在项目根运行，读取默认 scenarios.json，写默认 report.json
        default_report = PROJECT_ROOT / "report.json"
        if default_report.exists():
            default_report.unlink()
        proc_default = subprocess.run(
            [PYTHON, "-I", str(PROJECT_ROOT / "checker.py"),
             "--vuln-url", f"http://127.0.0.1:{vuln.port}",
             "--fixed-url", f"http://127.0.0.1:{fixed.port}"],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        default_ok = proc_default.returncode in (0, 4, 5) and default_report.exists()

        # (b) 自定义清单与输出路径
        custom_report = OUTPUT_DIR / "custom_path_report.json"
        proc_custom = run_checker(
            PROJECT_ROOT / "scenarios" / "unordered_manifest.json",
            custom_report, vuln.port, fixed.port,
        )
        custom_data = load_json(custom_report) if custom_report.exists() else {}
        custom_ok = (
            proc_custom.returncode in (0, 4, 5)
            and isinstance(custom_data, dict)
            and custom_data.get("scenario_count") == 3
        )
        checks = {"无参数生成默认 report.json": default_ok,
                  "自定义路径加载对应清单": custom_ok}
        detail = (
            f"默认运行退出码={proc_default.returncode}，report.json 存在={default_report.exists()}\n"
            f"自定义运行退出码={proc_custom.returncode}，场景数="
            f"{custom_data.get('scenario_count') if isinstance(custom_data, dict) else 'N/A'}\n"
            f"断言: {_checks_str(checks)}"
        )
        self.record("验收2", "默认清单路径与命令行路径覆盖", all(checks.values()), detail)

    # ---- D3 双形态状态码与响应摘要 ----
    def case_d3_dual_responses(self) -> None:
        report = load_json(OUTPUT_DIR / "custom_path_report.json")
        first = report["scenarios"][0]
        vr, fr = first["vulnerable_response"], first["fixed_response"]
        checks = {
            "未修复形态有状态码": isinstance(vr.get("status_code"), int),
            "已修复形态有状态码": isinstance(fr.get("status_code"), int),
            "未修复形态有响应摘要": bool(vr.get("body_excerpt")),
            "已修复形态有响应摘要": bool(fr.get("body_excerpt")),
            "两侧请求路径与查询一致":
                urllib.parse.urlsplit(vr.get("url", "")).path
                == urllib.parse.urlsplit(fr.get("url", "")).path
                and urllib.parse.urlsplit(vr.get("url", "")).query
                == urllib.parse.urlsplit(fr.get("url", "")).query,
        }
        detail = (
            f"场景 {first['id']}:\n"
            f"  未修复: HTTP {vr['status_code']} 摘要={vr['body_excerpt'][:80]!r}\n"
            f"  已修复: HTTP {fr['status_code']} 摘要={fr['body_excerpt'][:80]!r}\n"
            f"断言: {_checks_str(checks)}"
        )
        self.record("验收3", "同一场景双形态状态码与响应摘要", all(checks.values()), detail)

    # ---- D4 越权：修复前泄露、修复后不泄露 ----
    def case_d4_leak_then_fix(self) -> None:
        report = load_json(PROJECT_ROOT / "report.json")
        target = next(
            s for s in report["scenarios"] if s["id"] == "S01-bob-reads-alice-resource"
        )
        vr, fr = target["vulnerable_response"], target["fixed_response"]
        leaked_before = target["verdict"]["evidence"]["leaked_fields_in_vulnerable"]
        leaked_after = target["verdict"]["evidence"]["leaked_fields_in_fixed"]
        checks = {
            "修复前 200 且泄露敏感字段": vr["status_code"] == 200 and bool(leaked_before),
            "修复后 403 且无敏感字段": fr["status_code"] == 403 and leaked_after == [],
            "结论为已修复": target["verdict"]["code"] == "fixed",
        }
        detail = (
            f"修复前 HTTP {vr['status_code']} 泄露字段={leaked_before}\n"
            f"修复前响应摘要: {vr['body_excerpt'][:120]!r}\n"
            f"修复后 HTTP {fr['status_code']} 泄露字段={leaked_after}\n"
            f"修复后响应摘要: {fr['body_excerpt'][:120]!r}\n"
            f"判定依据: {target['verdict']['reason']}\n"
            f"断言: {_checks_str(checks)}"
        )
        self.record("验收4", "他人资源访问：修复前泄露、修复后不泄露",
                    all(checks.values()), detail)

    # ---- D5 三类结论与依据 ----
    def case_d5_three_verdicts(self) -> None:
        report = load_json(PROJECT_ROOT / "report.json")
        codes = {s["verdict"]["code"] for s in report["scenarios"]}
        reason_ok = all(
            s["verdict"]["code"] in ("fixed", "vulnerable", "cannot_determine")
            and isinstance(s["verdict"].get("reason"), str)
            and len(s["verdict"]["reason"].strip()) > 10
            for s in report["scenarios"]
        )
        checks = {
            "结论均属于三类之一": codes <= {"fixed", "vulnerable", "cannot_determine"},
            "每条均含人类可读依据": reason_ok,
            "默认清单含无法判定样本(S07)": "cannot_determine" in codes,
        }
        per_line = "\n".join(
            f"  {s['id']}: {s['verdict']['label']} — {s['verdict']['reason'][:70]}"
            for s in report["scenarios"]
        )
        detail = f"逐场景结论:\n{per_line}\n断言: {_checks_str(checks)}"
        self.record("验收5", "三类结论且每条附依据", all(checks.values()), detail)

    # ---- D6 暂时性故障后重试，不丢弃场景 ----
    def case_d6_retry(self) -> None:
        # flaky-first=2：健康检查 /health 不计数，前两个业务请求 503，
        # 保证首个场景的第一次尝试必然命中暂时性故障，重试后恢复
        flaky = ServerProcess(
            "flaky-vulnerable", PORT_D6_FLAKY, "vulnerable",
            extra_args=["--flaky-first", "2"],
        )
        flaky.start()
        self.servers.append(flaky)
        try:
            report_path = OUTPUT_DIR / "retry_report.json"
            proc = run_checker(
                PROJECT_ROOT / "scenarios.json", report_path,
                PORT_D6_FLAKY, PORT_FIXED, retries=2,
            )
            report = load_json(report_path)
            first = report["scenarios"][0]
            vuln_obs = first["vulnerable_response"]
            checks = {
                "首个场景未修复形态发生过重试": vuln_obs["attempts"] >= 2,
                "首次尝试命中503": vuln_obs["attempts_detail"][0]["status_code"] == 503,
                "重试后取得200": vuln_obs["status_code"] == 200 and vuln_obs["available"],
                "场景仍产出结论": first["verdict"]["code"] in ("fixed", "vulnerable", "cannot_determine"),
            }
            detail = (
                f"退出码={proc.returncode}\n尝试明细={json.dumps(vuln_obs['attempts_detail'], ensure_ascii=False)}\n"
                f"最终状态码={vuln_obs['status_code']}，结论={first['verdict']['label']}\n"
                f"断言: {_checks_str(checks)}"
            )
            self.record("验收6", "暂时性故障自动重试且不丢弃场景",
                        all(checks.values()), detail, proc.returncode)
        finally:
            flaky.stop()
            self.servers.remove(flaky)

    # ---- D7 形态持续不可用：显式列出，其余照常 ----
    def case_d7_persistent_down(self, fixed: ServerProcess) -> None:
        # fixed 形态指向全程无服务的 PORT_DEAD；另准备一份含属主场景的清单
        report_path = OUTPUT_DIR / "persistent_down_report.json"
        proc = run_checker(
            PROJECT_ROOT / "scenarios.json", report_path,
            PORT_VULN, PORT_DEAD, retries=1,
        )
        report = load_json(report_path)
        unavailable = report["unavailable_forms"]
        cross = next(s for s in report["scenarios"]
                     if s["id"] == "S04-alice-reads-own-resource")
        cross_other = next(s for s in report["scenarios"]
                           if s["id"] == "S01-bob-reads-alice-resource")
        cannot_ids = report["overall_conclusion"]["cannot_determine_scenario_ids"]
        checks = {
            "报告显式列出不可用形态": any(
                u["form"] == "fixed" and u["failure_count"] == report["scenario_count"]
                for u in unavailable
            ),
            "属主场景仍有结论": cross["verdict"]["code"] == "fixed",
            "越权场景标记无法判定而非误报": cross_other["verdict"]["code"] == "cannot_determine",
            "无法判定项被整体结论单列":
                "S01-bob-reads-alice-resource" in cannot_ids
                and "S07-reads-unknown-resource" in cannot_ids,
        }
        detail = (
            f"退出码={proc.returncode}\nunavailable_forms={json.dumps(unavailable, ensure_ascii=False)}\n"
            f"属主场景 S04 结论={cross['verdict']['label']}\n"
            f"越权场景 S01 结论={cross_other['verdict']['label']}（依据: {cross_other['verdict']['reason'][:60]}）\n"
            f"断言: {_checks_str(checks)}"
        )
        self.record("验收7", "持续不可用形态显式列出且其余场景照常完成",
                    all(checks.values()), detail, proc.returncode)

    # ---- D8 单场景持续失败：无法判定，不以空结果冒充 ----
    def case_d8_flaky_always(self) -> None:
        always = ServerProcess(
            "flaky-always-fixed", PORT_D8_ALWAYS, "fixed",
            extra_args=["--flaky-always", "res-1001"],
        )
        always.start()
        self.servers.append(always)
        try:
            report_path = OUTPUT_DIR / "flaky_always_report.json"
            proc = run_checker(
                PROJECT_ROOT / "scenarios.json", report_path,
                PORT_VULN, PORT_D8_ALWAYS, retries=2,
            )
            report = load_json(report_path)
            s01 = next(s for s in report["scenarios"]
                       if s["id"] == "S01-bob-reads-alice-resource")
            s04 = next(s for s in report["scenarios"]
                       if s["id"] == "S04-alice-reads-own-resource")
            checks = {
                "S01 重试3次仍失败": s01["fixed_response"]["attempts"] == 3
                and all(a["status_code"] == 503 for a in s01["fixed_response"]["attempts_detail"]),
                "S01 结论为无法判定": s01["verdict"]["code"] == "cannot_determine",
                "S01 未被误报为已修复": s01["verdict"]["code"] != "fixed",
                "其他场景 S04 正常判定": s04["verdict"]["code"] == "fixed",
            }
            detail = (
                f"退出码={proc.returncode}\nS01 已修复形态尝试明细="
                f"{json.dumps(s01['fixed_response']['attempts_detail'], ensure_ascii=False)}\n"
                f"S01 结论={s01['verdict']['label']}（{s01['verdict']['reason'][:70]}）\n"
                f"S04 结论={s04['verdict']['label']}\n断言: {_checks_str(checks)}"
            )
            self.record("验收8", "失败场景标记无法判定而非空结果冒充成功",
                        all(checks.values()), detail, proc.returncode)
        finally:
            always.stop()
            self.servers.remove(always)

    # ---- D9 混合三类：整体未完全修复 + 单列无法判定 ----
    def case_d9_mixed_overall(self) -> None:
        # 独立端口，避免与其他用例的服务实例生命周期相互干扰：
        # 18081 纯 vulnerable 充当“未修复侧”（全部接口越权）；
        # 18083 fixed-partial 充当“已修复侧”：资源接口已修复但账号接口仍越权
        #   -> S03 账号越权：已修复侧仍泄露 ssn/salary，判定“漏洞仍存在”
        # 18084 真正 fixed 但对 res-1002 持续 503，替换已修复侧以制造无法判定项：
        #   S02 资源场景已修复侧不可用 -> “无法判定”
        partial = ServerProcess("partial-fixed", PORT_PARTIAL, "fixed-partial")
        always = ServerProcess(
            "partial-flaky-always", PORT_ALWAYS, "fixed",
            extra_args=["--flaky-always", "res-1002"],
        )
        partial.start()
        always.start()
        self.servers.extend([partial, always])
        try:
            report_path = OUTPUT_DIR / "mixed_report.json"
            # 自定义一份 D9 专用清单：账号越权场景打 partial 端口组，
            # 资源场景打带 flaky-always 的 fixed 端口组。
            # 简化做法：分两次运行后合并断言不现实，因此单清单 + 混合形态编排：
            # 未修复侧=vulnerable(18081)；已修复侧=partial(18083)，
            # 再单独对 always(18084) 跑 S02 验证无法判定路径。
            proc_partial = run_checker(
                PROJECT_ROOT / "scenarios.json", report_path,
                PORT_VULN, PORT_PARTIAL, retries=1,
            )
            report = load_json(report_path)
            overall = report["overall_conclusion"]
            code_set = {s["verdict"]["code"] for s in report["scenarios"]}
            s03 = next(s for s in report["scenarios"]
                       if s["id"] == "S03-bob-reads-alice-account")

            # 第二轮：已修复侧换成带持续故障的真 fixed，验证无法判定单列
            report_path2 = OUTPUT_DIR / "mixed_report_cannot.json"
            proc_cannot = run_checker(
                PROJECT_ROOT / "scenarios.json", report_path2,
                PORT_VULN, PORT_ALWAYS, retries=1,
            )
            report2 = load_json(report_path2)
            overall2 = report2["overall_conclusion"]
            s02 = next(s for s in report2["scenarios"]
                       if s["id"] == "S02-carol-reads-bob-resource")
            checks = {
                "部分修复运行整体结论为未完全修复":
                    overall["code"] == "not_fully_fixed",
                "账号越权 S03 判为漏洞仍存在":
                    s03["verdict"]["code"] == "vulnerable",
                "漏洞场景 id 被列出":
                    "S03-bob-reads-alice-account" in overall["vulnerable_scenario_ids"],
                "故障运行存在无法判定并单列":
                    "S02-carol-reads-bob-resource"
                    in overall2["cannot_determine_scenario_ids"],
                "资源场景 S02 因形态不可用判无法判定":
                    s02["verdict"]["code"] == "cannot_determine",
                "故障运行退出码非零": proc_cannot.returncode in (4, 5),
                "部分修复运行退出码非零": proc_partial.returncode in (4, 5),
            }
            detail = (
                f"部分修复运行退出码={proc_partial.returncode}；整体={overall['label']} — {overall['summary']}\n"
                f"漏洞仍存在项={overall['vulnerable_scenario_ids']}\n"
                f"S03 依据: {s03['verdict']['reason'][:90]}\n"
                f"故障运行退出码={proc_cannot.returncode}；无法判定项={overall2['cannot_determine_scenario_ids']}\n"
                f"S02 依据: {s02['verdict']['reason'][:90]}\n"
                f"断言: {_checks_str(checks)}"
            )
            self.record("验收9", "混合三类场景的整体汇总与无法判定单列",
                        all(checks.values()), detail, proc_partial.returncode)
        finally:
            always.stop()
            self.servers.remove(always)
            partial.stop()
            self.servers.remove(partial)

    # ---- D10 顺序保持 ----
    def case_d10_order(self) -> None:
        report = load_json(OUTPUT_DIR / "custom_path_report.json")
        manifest = load_json(PROJECT_ROOT / "scenarios" / "unordered_manifest.json")
        input_ids = [s["id"] for s in manifest]
        output_ids = [s["id"] for s in report["scenarios"]]
        orders = [s["order"] for s in report["scenarios"]]
        checks = {
            "输出顺序与清单一致": output_ids == input_ids,
            "order 字段连续": orders == [1, 2, 3],
        }
        detail = (
            f"输入顺序={input_ids}\n输出顺序={output_ids}\n断言: {_checks_str(checks)}"
        )
        self.record("验收10", "报告场景顺序与清单严格一致", all(checks.values()), detail)

    # ---- D11 报告 JSON 字段 ----
    def case_d11_report_fields(self) -> None:
        report = load_json(PROJECT_ROOT / "report.json")
        required_top = ["generated_at", "tool_version", "scenarios", "overall_conclusion"]
        checks = {
            "顶层字段齐全": all(k in report for k in required_top),
            "含生成时间": isinstance(report.get("generated_at"), str)
            and "T" in report["generated_at"],
            "含工具版本": report.get("tool_version") == "1.0.0",
            "含逐场景结论": isinstance(report.get("scenarios"), list)
            and all("verdict" in s for s in report["scenarios"]),
            "含整体结论": isinstance(report.get("overall_conclusion"), dict)
            and "code" in report["overall_conclusion"],
            "文件本身为合法 JSON": True,
        }
        detail = (
            f"generated_at={report.get('generated_at')}\n"
            f"tool_version={report.get('tool_version')}\n"
            f"顶层键={list(report.keys())}\n断言: {_checks_str(checks)}"
        )
        self.record("验收11", "报告为合法 JSON 且含时间/版本/两类结论",
                    all(checks.values()), detail)

    # ---- D12 空清单 ----
    def case_d12_empty(self) -> None:
        report = OUTPUT_DIR / "should_not_exist_d12.json"
        if report.exists():
            report.unlink()
        proc = run_checker(
            PROJECT_ROOT / "scenarios" / "empty_manifest.json",
            report, PORT_VULN, PORT_FIXED,
        )
        checks = {
            "退出码非零": proc.returncode != 0,
            "提示无场景": "空数组" in proc.stdout or "无场景" in proc.stdout,
            "未写出报告": not report.exists(),
        }
        detail = (
            f"退出码={proc.returncode}\n输出:\n{_indent(proc.stdout.strip())}\n"
            f"断言: {_checks_str(checks)}"
        )
        self.record("验收12", "空清单非零退出且不写报告", all(checks.values()),
                    detail, proc.returncode)

    # ---- D13 1000 条场景 ----
    def case_d13_scale(self) -> None:
        # 独立端口的专用服务实例，避免与前面故障注入用例的长生命周期服务争用
        scale_vuln = ServerProcess("scale-vulnerable", PORT_SCALE_VULN, "vulnerable")
        scale_fixed = ServerProcess("scale-fixed", PORT_SCALE_FIXED, "fixed")
        scale_vuln.start()
        scale_fixed.start()
        self.servers.extend([scale_vuln, scale_fixed])
        try:
            big_manifest = OUTPUT_DIR / "scenarios_1000.json"
            scenarios = []
            # 确定性构造：500 条跨用户越权 + 500 条属主访问，共 1000 条
            for i in range(500):
                scenarios.append({
                    "id": f"BIG-CROSS-{i:04d}",
                    "actor": "bob",
                    "path": "/api/resource",
                    "query": {"resource_id": "res-1001"},
                })
            for i in range(500):
                scenarios.append({
                    "id": f"BIG-OWNER-{i:04d}",
                    "actor": "alice",
                    "path": "/api/resource",
                    "query": {"resource_id": "res-1001"},
                })
            with big_manifest.open("w", encoding="utf-8") as fh:
                json.dump(scenarios, fh, ensure_ascii=False)

            big_report = OUTPUT_DIR / "report_1000.json"
            start = time.time()
            # 本地回环正常无故障：8 线程并发、超时 5s、不重试
            proc = subprocess.run(
                [
                    PYTHON, "-I", str(PROJECT_ROOT / "checker.py"),
                    str(big_manifest),
                    "--output", str(big_report),
                    "--vuln-url", f"http://127.0.0.1:{scale_vuln.port}",
                    "--fixed-url", f"http://127.0.0.1:{scale_fixed.port}",
                    "--retries", "0", "--retry-delay", "0.1", "--timeout", "5",
                    "--workers", "8", "--quiet",
                ],
                cwd=PROJECT_ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300,
            )
            elapsed = time.time() - start
            report = load_json(big_report)
            ids = [s["id"] for s in report["scenarios"]]
            checks = {
                "退出码为0(全部已修复)": proc.returncode == 0,
                "场景数为1000": report["scenario_count"] == 1000,
                "无缺失条目": len(report["scenarios"]) == 1000 and len(set(ids)) == 1000,
                "全部判定为fixed": all(s["verdict"]["code"] == "fixed" for s in report["scenarios"]),
                "顺序与清单一致": [s["order"] for s in report["scenarios"]] == list(range(1, 1001)),
                "无缺失库错误": "ModuleNotFoundError" not in proc.stderr
                and "ImportError" not in proc.stderr,
            }
            detail = (
                f"退出码={proc.returncode}，耗时={elapsed:.1f}s，条目数={len(ids)}\n"
                f"stderr 摘要={proc.stderr.strip()[:120] or '(空)'}\n"
                f"断言: {_checks_str(checks)}"
            )
            self.record("验收13", "Windows 标准库下 1000 条场景完整判定",
                        all(checks.values()), detail, proc.returncode)
        finally:
            scale_fixed.stop()
            self.servers.remove(scale_fixed)
            scale_vuln.stop()
            self.servers.remove(scale_vuln)

    def run_all(self) -> bool:
        OUTPUT_DIR.mkdir(exist_ok=True)
        busy = [p for p in ALL_USED_PORTS if port_in_use(p)]
        if busy:
            raise RuntimeError(f"端口 {busy} 被占用，请先释放后重试")

        vuln = ServerProcess("vulnerable", PORT_VULN, "vulnerable")
        fixed = ServerProcess("fixed", PORT_FIXED, "fixed")
        vuln.start()
        fixed.start()
        self.servers.extend([vuln, fixed])
        try:
            self.case_d1_invalid_manifest()
            self.case_d2_paths(vuln, fixed)
            self.case_d3_dual_responses()
            self.case_d4_leak_then_fix()
            self.case_d5_three_verdicts()
            self.case_d6_retry()
            self.case_d7_persistent_down(fixed)
            self.case_d8_flaky_always()
            self.case_d9_mixed_overall()
            self.case_d10_order()
            self.case_d11_report_fields()
            self.case_d12_empty()
            self.case_d13_scale()
        finally:
            for server in list(self.servers):
                server.stop()

        passed = sum(1 for r in self.results if r.passed)
        summary = {
            "total": len(self.results),
            "passed": passed,
            "failed": len(self.results) - passed,
            "cases": [
                {
                    "requirement": r.requirement,
                    "title": r.title,
                    "passed": r.passed,
                    "exit_code": r.exit_code,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }
        with (OUTPUT_DIR / "results_summary.json").open("w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
        log("")
        log(f"汇总: {passed}/{len(self.results)} 项验收通过，详见 "
            f"demo_output/results_summary.json")
        return passed == len(self.results)


def _checks_str(checks: dict[str, bool]) -> str:
    return "; ".join(f"{k}={'是' if v else '否'}" for k, v in checks.items())


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines())


def main() -> int:
    log("=" * 78)
    log("本地授权越权靶场 — 13 项验收自动化演示（仅标准库，无容器，全部为本地样本）")
    log("=" * 78)
    suite = DemoSuite()
    try:
        ok = suite.run_all()
    except Exception as exc:  # noqa: BLE001
        log(f"[致命错误] {type(exc).__name__}: {exc}")
        for server in suite.servers:
            server.stop()
        return 1
    return 0 if ok else 6


if __name__ == "__main__":
    raise SystemExit(main())
