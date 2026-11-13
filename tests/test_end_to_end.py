"""端到端测试：自动在本机回环端口启动脆弱版/修复版演示站点。

直接运行: python -m unittest tests.test_end_to_end -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from demo_site.server import build_handler
from xss_probe.models import (
    CTX_ATTR,
    CTX_SCRIPT,
    CTX_TEXT,
    ENTRY_FORM,
    ENTRY_PATH,
    ENTRY_QUERY,
    RESULT_NO_RESPONSE,
    RESULT_RISK,
    RESULT_SAFE,
)
from xss_probe.netutil import NotLoopbackError, assert_loopback_url
from xss_probe.parserutil import classify_contexts, escaped_forms
from xss_probe.report import compare_reports
from xss_probe.samples import build_history_sample, build_sample_cases
from xss_probe.scanner import run_scan
from xss_probe.report import build_report, load_cases

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _start_server(mode: str) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(mode))
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base


class EndToEndTest(unittest.TestCase):
    vuln_server = None
    fixed_server = None
    vuln_base = ""
    fixed_base = ""
    tmpdir: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        cls.vuln_server, cls.vuln_base = _start_server("vulnerable")
        cls.fixed_server, cls.fixed_base = _start_server("fixed")
        cls.tmpdir = tempfile.TemporaryDirectory(prefix="xss_probe_test_")
        cls.cases = load_cases_from_samples()
        # 确认两个服务确实可服务
        for base in (cls.vuln_base, cls.fixed_base):
            with urllib.request.urlopen(base + "/safe?q=ok", timeout=5) as resp:
                resp.read()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.vuln_server.shutdown()
        cls.fixed_server.shutdown()
        cls.tmpdir.cleanup()

    def result_map(self, base: str) -> dict[str, dict]:
        records = run_scan(base, self.cases)
        return {r["用例标识"]: r for r in records}

    # ---- 验收 1：非回环地址拒绝 ----
    def test_01_external_address_rejected(self) -> None:
        for url in ("http://example.com/", "http://8.8.8.8:8080/",
                    "https://192.168.1.10/"):
            with self.assertRaises(NotLoopbackError):
                assert_loopback_url(url)
        assert_loopback_url("http://127.0.0.1:8033/")  # 回环不拒绝
        assert_loopback_url("http://localhost:8033/")

    # ---- 验收 2：三类入口各至少一条 ----
    def test_02_three_entry_types_covered(self) -> None:
        records = run_scan(self.vuln_base, self.cases)
        entry_types = {r["入口类型"] for r in records}
        self.assertEqual(
            entry_types, {ENTRY_QUERY, ENTRY_PATH, ENTRY_FORM}
        )
        markers = [r["标记"] for r in records]
        self.assertEqual(len(markers), len(set(markers)), "标记必须唯一")

    # ---- 验收 3：未转义回显判存在风险 ----
    def test_03_unescaped_echo_is_risk(self) -> None:
        results = self.result_map(self.vuln_base)
        for case_id in ("TC-001", "TC-002", "TC-003", "TC-004", "TC-005"):
            self.assertEqual(results[case_id]["结论"], RESULT_RISK, case_id)

    # ---- 验收 4：转义后回显判无风险 ----
    def test_04_escaped_echo_is_safe(self) -> None:
        results = self.result_map(self.fixed_base)
        for case_id in ("TC-001", "TC-002", "TC-004", "TC-005", "TC-007"):
            self.assertEqual(results[case_id]["结论"], RESULT_SAFE, case_id)
        # 标记在响应里确实以转义形式存在，而非没回显
        self.assertIn("转义", results["TC-001"]["说明"])

    # ---- 验收 5：三类上下文标注 ----
    def test_05_context_classification(self) -> None:
        results = self.result_map(self.vuln_base)
        self.assertEqual(results["TC-001"]["上下文类型"], CTX_TEXT)
        self.assertEqual(results["TC-002"]["上下文类型"], CTX_ATTR)
        self.assertEqual(results["TC-003"]["上下文类型"], CTX_SCRIPT)
        self.assertEqual(results["TC-004"]["上下文类型"], CTX_TEXT)
        self.assertEqual(results["TC-005"]["上下文类型"], CTX_TEXT)

    # ---- 验收 6/12：空体与不可达记未获得有效响应，不影响其他用例 ----
    def test_06_empty_body_is_no_valid_response(self) -> None:
        results = self.result_map(self.vuln_base)
        self.assertEqual(results["TC-006"]["结论"], RESULT_NO_RESPONSE)
        self.assertEqual(results["TC-006"]["上下文类型"], "不适用")
        self.assertEqual(results["TC-008"]["结论"], RESULT_NO_RESPONSE)
        self.assertIn("请求失败", results["TC-008"]["说明"])
        # 其余用例结论不受影响
        self.assertEqual(results["TC-001"]["结论"], RESULT_RISK)
        self.assertEqual(results["TC-007"]["结论"], RESULT_SAFE)

    # ---- 验收 7：报告字段齐全、结论取值合法 ----
    def test_07_report_schema(self) -> None:
        records = run_scan(self.vuln_base, self.cases)
        report = build_report(self.vuln_base, "vulnerable", records)
        allowed = {"存在风险", "未获得有效响应", "无风险"}
        for record in report["用例记录"]:
            for field in ("入口类型", "用例标识", "标记", "结论", "上下文类型"):
                self.assertIn(field, record)
            self.assertIn(record["结论"], allowed)
        self.assertEqual(report["修复状态标识"], "vulnerable")

    # ---- 验收 8：排序规则 ----
    def test_08_report_ordering(self) -> None:
        records = run_scan(self.vuln_base, self.cases)
        order = [r["结论"] for r in records]
        self.assertEqual(order[0], RESULT_RISK)
        self.assertEqual(order[-1], RESULT_SAFE)
        # 同组内按用例标识升序
        risk_ids = [r["用例标识"] for r in records if r["结论"] == RESULT_RISK]
        self.assertEqual(risk_ids, sorted(risk_ids))
        no_resp_ids = [r["用例标识"] for r in records
                       if r["结论"] == RESULT_NO_RESPONSE]
        self.assertEqual(no_resp_ids, sorted(no_resp_ids))

    # ---- 验收 9：单条失败不影响其余用例 ----
    def test_09_single_failure_isolation(self) -> None:
        records = run_scan(self.vuln_base, self.cases)
        self.assertEqual(len(records), len(self.cases))
        conclusions = {r["用例标识"]: r["结论"] for r in records}
        self.assertEqual(conclusions["TC-008"], RESULT_NO_RESPONSE)
        # 其余 7 条都各自拿到了明确结论
        for case_id in ("TC-001", "TC-002", "TC-003", "TC-004",
                        "TC-005", "TC-006", "TC-007"):
            self.assertIn(conclusions[case_id],
                          (RESULT_RISK, RESULT_SAFE, RESULT_NO_RESPONSE))

    # ---- 验收 10：重复运行结论完全一致 ----
    def test_10_deterministic_runs(self) -> None:
        first = build_report(self.vuln_base, "vulnerable",
                             run_scan(self.vuln_base, self.cases))
        second = build_report(self.vuln_base, "vulnerable",
                              run_scan(self.vuln_base, self.cases))
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(first["运行标识"], second["运行标识"])

    # ---- 验收 11：历史比对，组合键匹配新增/消除 ----
    def test_11_history_comparison(self) -> None:
        history = build_history_sample()
        current = build_report(self.vuln_base, "vulnerable",
                               run_scan(self.vuln_base, self.cases))
        diff = compare_reports(history, current)
        # TC-005 表单风险为新增；TC-099（旧入口）与历史 4 条中的差异体现消除
        new_ids = {r["用例标识"] for r in diff["新增风险"]}
        resolved_ids = {r["用例标识"] for r in diff["已消除风险"]}
        self.assertIn("TC-005", new_ids)
        self.assertIn("TC-099", resolved_ids)

        # 修复版对比历史：历史风险全部消除，无新增
        fixed = build_report(self.fixed_base, "fixed",
                             run_scan(self.fixed_base, self.cases))
        diff_fixed = compare_reports(history, fixed)
        self.assertEqual(diff_fixed["新增风险数"], 0)
        resolved_fixed = {r["用例标识"] for r in diff_fixed["已消除风险"]}
        self.assertEqual(
            resolved_fixed, {"TC-001", "TC-002", "TC-003", "TC-004", "TC-099"}
        )

    # ---- 验收 13：汇总计数与记录一致 ----
    def test_13_summary_counts(self) -> None:
        records = run_scan(self.vuln_base, self.cases)
        report = build_report(self.vuln_base, "vulnerable", records)
        s = report["汇总"]
        self.assertEqual(s["总用例数"], len(records))
        self.assertEqual(s["存在风险数"],
                         sum(1 for r in records if r["结论"] == RESULT_RISK))
        self.assertEqual(s["未获得有效响应数"],
                         sum(1 for r in records if r["结论"] == RESULT_NO_RESPONSE))
        self.assertEqual(
            s["无风险数"],
            s["总用例数"] - s["存在风险数"] - s["未获得有效响应数"],
        )

    # ---- 验收 13：CLI 退出码反映风险 ----
    def test_13_cli_exit_codes(self) -> None:
        cases_path = Path(self.tmpdir.name) / "cases.json"
        cases_path.write_text(
            json.dumps(build_sample_cases(), ensure_ascii=False), encoding="utf-8"
        )

        def invoke(base: str, report_name: str, extra=None) -> subprocess.CompletedProcess:
            cmd = [
                sys.executable, "-X", "utf8", "-m", "xss_probe", "scan",
                "--base-url", base,
                "--cases", str(cases_path),
                "--report", str(Path(self.tmpdir.name) / report_name),
                "--fix-status", "demo",
            ]
            if extra:
                cmd.extend(extra)
            # 显式 UTF-8 环境，避免子进程输出在非 UTF-8 默认编码（如 GBK）下解码失败
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            return subprocess.run(
                cmd, cwd=PROJECT_ROOT, capture_output=True, text=True,
                encoding="utf-8", env=env, timeout=60
            )

        vuln_run = invoke(self.vuln_base, "vuln.json")
        self.assertEqual(vuln_run.returncode, 1, vuln_run.stderr)
        self.assertIn("存在风险数", vuln_run.stdout)

        fixed_run = invoke(self.fixed_base, "fixed.json")
        self.assertEqual(fixed_run.returncode, 0, fixed_run.stderr)

        # 外部地址：非零退出并提示仅支持本机
        ext = invoke("http://example.com/", "ext.json")
        self.assertNotEqual(ext.returncode, 0)
        self.assertIn("回环", ext.stderr + ext.stdout)


class ParserUnitTest(unittest.TestCase):
    def test_text_attr_script_contexts(self) -> None:
        marker = 'z1qabc<svc>"1'
        self.assertEqual(
            classify_contexts(f"<div>{marker}</div>", marker), [CTX_TEXT]
        )
        self.assertEqual(
            classify_contexts(
                f'<input value="{marker}">', marker
            ),
            [CTX_ATTR],
        )
        self.assertEqual(
            classify_contexts(
                f"<script>var a = \"{marker}\";</script>", marker
            ),
            [CTX_SCRIPT],
        )

    def test_escaped_marker_not_classified_as_raw(self) -> None:
        marker = 'z1qabc<svc>"1'
        document = f"<div>{escaped_forms(marker)[0]}</div>"
        self.assertEqual(classify_contexts(document, marker), [])
        self.assertIn(escaped_forms(marker)[0], document)


def load_cases_from_samples():
    """直接从确定性样本构造用例对象，避免依赖临时文件。"""
    from xss_probe.models import TestCase

    return [
        TestCase(
            case_id=item["case_id"],
            entry_type=item["entry_type"],
            method=item["method"],
            path=item["path"],
            param=item["param"],
            marker=item["marker"],
            url=item.get("url"),
        )
        for item in build_sample_cases()
    ]


if __name__ == "__main__":
    unittest.main(verbosity=2)
