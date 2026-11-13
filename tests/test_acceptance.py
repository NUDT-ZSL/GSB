"""端到端验收测试：覆盖任务要求的 14 条验收标准。

运行方式（在项目根目录）：
  python -m unittest tests.test_acceptance -v
或：
  python tests/test_acceptance.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

# 允许以文件方式直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from local_injection_scanner.cli import main as cli_main  # noqa: E402
from local_injection_scanner.detector import (  # noqa: E402
    MAX_EVIDENCE_LEN,
    classify_response,
)
from local_injection_scanner.http_client import (  # noqa: E402
    NonLocalTargetError,
    assert_local_target,
)
from local_injection_scanner.payloads import PAYLOADS, category_count  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LabServer:
    """以子进程方式管理本地靶场服务。"""

    def __init__(self, mode: str, port: int, start_delay: float = 0.0):
        self.mode = mode
        self.port = port
        self.start_delay = start_delay
        self.proc: subprocess.Popen | None = None

    def start(self, wait: bool = True) -> None:
        cmd = [
            sys.executable, "-I",
            os.path.join(PROJECT_ROOT, "lab", "vuln_server.py"),
            "--mode", self.mode, "--port", str(self.port),
            "--host", "127.0.0.1",
        ]
        if self.start_delay:
            cmd += ["--start-delay", str(self.start_delay)]
        self.proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if wait:
            self._wait_until_up(timeout=15)

    def _wait_until_up(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"靶场进程意外退出:\n{out}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.2)
        raise TimeoutError("靶场服务启动超时")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main(argv)
    return code, buf.getvalue()


class AcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.mkdtemp(prefix="inj_scan_test_")
        cls.vuln_port = free_port()
        cls.fixed_port = free_port()
        cls.vuln = LabServer("vulnerable", cls.vuln_port)
        cls.fixed = LabServer("fixed", cls.fixed_port)
        cls.vuln.start()
        cls.fixed.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.vuln.stop()
        cls.fixed.stop()

    def _paths(self, name: str) -> tuple[str, str]:
        return (
            os.path.join(self.tmpdir, f"{name}.json"),
            os.path.join(self.tmpdir, f"{name}.log"),
        )

    # ---- 验收 1 & 2 & 3 & 8 & 12 & 14(退出码1)：漏洞服务完整扫描 ----
    def test_01_vulnerable_service_full_scan(self) -> None:
        report_path, log_path = self._paths("vuln")
        target = f"http://127.0.0.1:{self.vuln_port}"
        code, console = run_cli([
            "--target", target, "--report", report_path,
            "--params", "username,item", "--log", log_path,
        ])

        # 验收 14：发现缺陷退出码 1
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(report_path))  # 验收 3：JSON 已生成

        report = json.load(open(report_path, encoding="utf-8"))
        findings = report["findings"]

        # 验收 1：指出参数名与缺陷类型
        vuln_params = {f["parameter"] for f in findings}
        self.assertIn("username", vuln_params)
        self.assertTrue(all(f["injection_type"] for f in findings))
        types = {f["injection_type"] for f in findings}
        self.assertIn("SQL注入", types)

        # 验收 2：载荷大类 >=5，且请求主机全部为本机
        categories = {r["payload_category"] for r in report["request_log"]
                      if r["payload_category"] != "reachability"}
        self.assertGreaterEqual(len(categories), 5)
        hosts = {r["host"] for r in report["request_log"]}
        self.assertTrue(hosts.issubset({"127.0.0.1", "localhost", "::1"}))

        # 验收 3：缺陷条目三要素齐全且证据非空
        for f in findings:
            self.assertTrue(f["parameter"])
            self.assertTrue(f["payload"])
            self.assertTrue(f["evidence"])

        # 验收 8：证据 <=500 字符，且与真实响应内容一致
        import urllib.error
        for f in findings:
            self.assertLessEqual(len(f["evidence"]), MAX_EVIDENCE_LEN)
            try:
                with urllib.request.urlopen(f["request_url"], timeout=5) as resp:
                    real_body = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                # 缺陷证据常来自 500 响应，读取错误响应体即可
                real_body = exc.read().decode("utf-8", errors="replace")
                exc.close()
            self.assertTrue(
                real_body.startswith(f["evidence"][:200])
                or f["evidence"][:200] in real_body
            )

        # 验收 12：控制台摘要含两个数量
        self.assertIn("已检测参数数", console)
        self.assertIn("发现缺陷数", console)
        self.assertIn("2", console)

        # 验收 13：日志含带时间戳的开始扫描记录
        log_text = open(log_path, encoding="utf-8").read()
        self.assertIn("开始扫描", log_text)
        self.assertRegex(log_text, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")

    # ---- 验收 6：单参数出错不中断其他参数 ----
    def test_06_parameter_error_isolation(self) -> None:
        report_path, _ = self._paths("isolation")
        target = f"http://127.0.0.1:{self.vuln_port}"
        code, _ = run_cli([
            "--target", target, "--report", report_path,
            "--params", "username,item",
        ])
        self.assertEqual(code, 1)
        report = json.load(open(report_path, encoding="utf-8"))
        by_param = {r["parameter"]: r for r in report["parameter_results"]}
        # item 的堆叠查询载荷触发普通 500：状态为 error，但 username 仍被检出
        self.assertEqual(by_param["item"]["status"], "error")
        self.assertGreater(by_param["item"]["request_errors"], 0)
        self.assertEqual(by_param["username"]["status"], "vulnerable")
        self.assertTrue(by_param["username"]["findings"])

    # ---- 验收 7 & 14(退出码0)：修复版无缺陷且无证据 ----
    def test_07_fixed_service_clean(self) -> None:
        report_path, _ = self._paths("fixed")
        target = f"http://127.0.0.1:{self.fixed_port}"
        code, console = run_cli([
            "--target", target, "--report", report_path,
            "--params", "username,item",
        ])
        self.assertEqual(code, 0)  # 验收 14：无缺陷退出码 0
        report = json.load(open(report_path, encoding="utf-8"))
        self.assertEqual(report["findings"], [])           # 证据列表为空
        self.assertEqual(report["summary"]["findings_count"], 0)
        self.assertIn("未发现缺陷", report["conclusion"])
        self.assertEqual(
            sorted(report["summary"]["safe_parameters"]), ["item", "username"]
        )

    # ---- 验收 5 & 14：服务持续不可达 ----
    def test_05_service_unreachable(self) -> None:
        dead_port = free_port()  # 无人监听
        report_path, _ = self._paths("dead")
        target = f"http://127.0.0.1:{dead_port}"
        code, console = run_cli([
            "--target", target, "--report", report_path,
            "--params", "username,item",
        ])
        self.assertEqual(code, 3)
        report = json.load(open(report_path, encoding="utf-8"))
        self.assertFalse(report["service_reachable"])
        self.assertIn("不可达", report["conclusion"])
        # 不得把任何参数判为安全
        self.assertEqual(report["summary"]["safe_parameters"], [])
        self.assertEqual(report["findings"], [])
        self.assertIn("服务可达      : 否", console)

    # ---- 验收 4：延迟探测，等待期内服务启动后仍完成检测 ----
    def test_04_delay_then_service_comes_up(self) -> None:
        port = free_port()
        delayed = LabServer("vulnerable", port, start_delay=4)
        report_path, _ = self._paths("delay")
        try:
            delayed.start(wait=False)  # 进程已起，但 HTTP 4 秒后才监听
            # 确认扫描开始前端口确实未监听
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            target = f"http://127.0.0.1:{port}"
            started = time.monotonic()
            code, _ = run_cli([
                "--target", target, "--report", report_path,
                "--params", "username", "--delay", "12",
            ])
            elapsed = time.monotonic() - started
            self.assertEqual(code, 1)
            report = json.load(open(report_path, encoding="utf-8"))
            self.assertTrue(report["service_reachable"])
            self.assertGreater(report["summary"]["findings_count"], 0)
            # 确实经历了等待（服务约 4 秒后才监听），但在超时前完成
            self.assertGreaterEqual(elapsed, 3)
            self.assertLess(elapsed, 11)
        finally:
            delayed.stop()

    # ---- 验收 11：非本机地址拒绝 ----
    def test_11_non_local_target_rejected(self) -> None:
        report_path, _ = self._paths("external")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = cli_main([
                "--target", "http://8.8.8.8:8000", "--report", report_path,
            ])
        self.assertEqual(code, 2)
        self.assertIn("仅支持本地目标", buf.getvalue())
        self.assertFalse(os.path.exists(report_path))  # 被拒绝时不产生报告

    # ---- 验收 9 & 14(退出码2)：无参数显示用法；非法格式退出码 2 ----
    def test_09_usage_and_invalid_format(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code_no_args = cli_main([])
        usage = buf.getvalue()
        self.assertEqual(code_no_args, 2)
        self.assertIn("--target", usage)
        self.assertIn("--report", usage)
        self.assertIn("--delay", usage)

        report_path, _ = self._paths("badurl")
        buf2 = io.StringIO()
        with contextlib.redirect_stderr(buf2):
            code_bad = cli_main(["--target", "ftp://127.0.0.1/x", "--report", report_path])
        self.assertEqual(code_bad, 2)
        self.assertIn("目标错误", buf2.getvalue())

    # ---- 验收 10：普通 200 不报，数据库错误才报（检测器单元测试） ----
    def test_10_normal_vs_injected_response(self) -> None:
        normal = classify_response("anything", '{"ok": true, "rows": []}', 200, False,
                                   payload_category="sql_error")
        self.assertFalse(normal.is_vulnerable)

        db_error = classify_response(
            "'", "sqlite3.OperationalError: SQL syntax error", 500, False,
            payload_category="sql_error",
        )
        self.assertTrue(db_error.is_vulnerable)
        self.assertEqual(db_error.injection_type, "SQL注入")

        # 5xx 但无注入特征 -> 请求错误而非缺陷
        plain_500 = classify_response("x", "internal failure", 500, False,
                                      payload_category="sql_stacked")
        self.assertFalse(plain_500.is_vulnerable)
        self.assertTrue(plain_500.request_error)

        # 命令注入载荷即使含分号，也按大类判定，不被 SQL 签名串扰
        cmd = classify_response("; id", "uid=0(root) gid=0(root)", 200, False,
                                payload_category="cmd_injection")
        self.assertTrue(cmd.is_vulnerable)
        self.assertEqual(cmd.injection_type, "命令注入")

        # 纯文本回显 onerror= 不构成 XSS；真实 <script> 反射才构成
        echo_only = classify_response("x", "搜索结果: onerror= 文本", 200, False,
                                     payload_category="xss_reflected")
        self.assertFalse(echo_only.is_vulnerable)
        reflected = classify_response("<script>alert(1)</script>",
                                      "页 <script>alert(1)</script> 尾", 200, False,
                                      payload_category="xss_reflected")
        self.assertTrue(reflected.is_vulnerable)
        self.assertEqual(reflected.injection_type, "反射型XSS")

    # ---- 验收 8（补充）：超长响应证据必须截断到 500 字符以内 ----
    def test_08_evidence_truncation(self) -> None:
        long_body = "prefix " + "sqlite3.OperationalError: SQL syntax error " * 50
        self.assertGreater(len(long_body), MAX_EVIDENCE_LEN)
        result = classify_response("'", long_body, 500, False, payload_category="sql_error")
        self.assertTrue(result.is_vulnerable)
        self.assertLessEqual(len(result.evidence), MAX_EVIDENCE_LEN)
        self.assertIn("截断", result.evidence)
        # 截断后的前缀必须与真实响应一致
        self.assertTrue(long_body.strip().startswith(result.evidence[:80]))

        # 短响应不追加截断标记
        short = classify_response("'", "SQL syntax error", 500, False,
                                  payload_category="sql_error")
        self.assertLessEqual(len(short.evidence), MAX_EVIDENCE_LEN)
        self.assertNotIn("截断", short.evidence)

    # ---- 附：本机校验与载荷库的独立单元测试 ----
    def test_unit_local_guard_and_payloads(self) -> None:
        self.assertEqual(assert_local_target("http://127.0.0.1:8000/query"), "127.0.0.1")
        self.assertEqual(assert_local_target("http://LOCALHOST:9000"), "localhost")
        self.assertEqual(assert_local_target("http://127.1.2.3:9000"), "127.1.2.3")
        with self.assertRaises(NonLocalTargetError):
            assert_local_target("http://example.com/")
        with self.assertRaises(NonLocalTargetError):
            assert_local_target("http://192.168.1.5/")
        with self.assertRaises(ValueError):
            assert_local_target("not-a-url")
        self.assertGreaterEqual(category_count(), 5)
        self.assertGreaterEqual(len(PAYLOADS), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
