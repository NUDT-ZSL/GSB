# -*- coding: utf-8 -*-
"""
端到端自动化测试：在临时目录与随机本机端口上拉起两版服务，驱动审计工具验证全部验收点。

运行方式（项目根目录）：
    python -I -m unittest tests.test_e2e -v
或：
    python -I tests\test_e2e.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "auditor"))
import auditor  # noqa: E402  (项目无第三方依赖，仅用于直接调用 main)

VULN_SERVER = os.path.join(PROJECT_ROOT, "vuln_server", "server_vuln.py")
FIXED_SERVER = os.path.join(PROJECT_ROOT, "fixed_server", "server_fixed.py")
RISK_RANK = {"高": 3, "中": 2, "低": 1}


def free_port() -> int:
    """申请一个当前空闲的本机端口（存在极小竞态，仅用于测试）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_health(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            time.sleep(0.2)
    return False


class ServerProcess:
    def __init__(self, script: str, upload_dir: str):
        self.port = free_port()
        self.upload_dir = upload_dir
        self.proc = subprocess.Popen(
            [sys.executable, "-I", script],
            cwd=PROJECT_ROOT,
            env=dict(os.environ, UPLOAD_DIR=upload_dir, PORT=str(self.port)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def healthy(self) -> bool:
        return wait_health(self.port)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class AuditorE2ETest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="project40_test_")
        self.upload_dir = os.path.join(self.workspace, "uploads")
        self.report_path = os.path.join(self.workspace, "report.json")
        os.makedirs(self.upload_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _run_auditor(self, port: int, clean_evidence: bool = False):
        argv = [
            "--url", f"http://127.0.0.1:{port}",
            "--dir", self.upload_dir,
            "--report", self.report_path,
        ]
        if clean_evidence:
            argv.append("--clean-evidence")
        rc = auditor.main(argv)
        report = None
        if os.path.isfile(self.report_path):
            with open(self.report_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            # UTF-8 可解码且 JSON 合法
            report = json.loads(raw)
        return rc, report

    def test_01_vulnerable_server_confirms_both_flaws(self):
        """验收点 1/2/3/4/5/6：漏洞版应报高危遍历（目录外确有文件）+ 中危类型绕过。"""
        server = ServerProcess(VULN_SERVER, self.upload_dir)
        self.addCleanup(server.stop)
        self.assertTrue(server.healthy(), "漏洞版服务启动失败")

        # 第一轮：保留证据，核实目录外文件真实落盘
        rc, report = self._run_auditor(server.port)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(report, "漏洞版扫描必须生成报告")

        self.assertEqual(report["summary"]["high_risk"], 1)
        self.assertEqual(report["summary"]["medium_risk"], 1)
        self.assertTrue(report["summary"]["vulnerable"])

        findings = {f["id"]: f for f in report["findings"]}
        self.assertIn("PATH_TRAVERSAL_WRITE", findings)
        self.assertIn("CONTENT_TYPE_BYPASS", findings)

        # 受影响路径必须是绝对路径，且遍历落点确实在上传目录之外并真实存在
        traversal = findings["PATH_TRAVERSAL_WRITE"]
        self.assertEqual(len(traversal["affected_paths"]), 3)
        upload_root = os.path.realpath(self.upload_dir)
        for path in traversal["affected_paths"]:
            self.assertTrue(os.path.isabs(path))
            self.assertTrue(os.path.isfile(path), f"越界证据文件应真实存在: {path}")
            self.assertNotEqual(os.path.commonpath([os.path.realpath(path), upload_root]), upload_root)

        # 类型绕过必须列出被错误接受的文件名
        bypass = findings["CONTENT_TYPE_BYPASS"]
        self.assertEqual(len(bypass["wrongly_accepted_filenames"]), 3)
        self.assertTrue(all(name.endswith((".png", ".jpg", ".pdf")) for name in bypass["wrongly_accepted_filenames"]))

        # 每条发现都含两类修复要点
        for finding in report["findings"]:
            self.assertIn("path_normalization", finding["remediation"])
            self.assertIn("content_type_validation", finding["remediation"])
            self.assertTrue(finding["remediation"]["path_normalization"].strip())
            self.assertTrue(finding["remediation"]["content_type_validation"].strip())

        # 风险等级降序排列（高在中、低之前）
        ranks = [RISK_RANK[f["risk_level"]] for f in report["findings"]]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

        # ISO 8601 时间戳可解析
        datetime.fromisoformat(report["generated_at"])
        for finding in report["findings"]:
            datetime.fromisoformat(finding["detected_at"])

        # 上传目录内无审计残留（证据只允许暂存在目录之外）
        self.assertEqual([n for n in os.listdir(self.upload_dir) if n.startswith("auditscan_")], [])
        evidence = [n for n in os.listdir(self.workspace) if n.startswith("auditscan_")]
        self.assertEqual(len(evidence), 3)

        # 第二轮：--clean-evidence 应删除目录外证据，且上传目录仍保持干净
        _rc2, report2 = self._run_auditor(server.port, clean_evidence=True)
        self.assertIsNotNone(report2)
        self.assertEqual([n for n in os.listdir(self.workspace) if n.startswith("auditscan_")], [])
        self.assertEqual([n for n in os.listdir(self.upload_dir) if n.startswith("auditscan_")], [])

    def test_02_fixed_server_regression_has_no_high_risk(self):
        """验收点 8：修复版无高/中危，上传目录外无新增文件。"""
        server = ServerProcess(FIXED_SERVER, self.upload_dir)
        self.addCleanup(server.stop)
        self.assertTrue(server.healthy(), "修复版服务启动失败")

        rc, report = self._run_auditor(server.port)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(report)
        self.assertEqual(report["summary"]["high_risk"], 0)
        self.assertEqual(report["summary"]["medium_risk"], 0)
        self.assertFalse(report["summary"]["vulnerable"])
        self.assertNotIn("PATH_TRAVERSAL_WRITE", {f["id"] for f in report["findings"]})
        self.assertNotIn("CONTENT_TYPE_BYPASS", {f["id"] for f in report["findings"]})

        # 沙箱根（上传目录的父目录）内除报告外不得有任何探测文件
        probe_outside = [n for n in os.listdir(self.workspace) if n.startswith("auditscan_")]
        self.assertEqual(probe_outside, [])
        leftovers = [n for n in os.listdir(self.upload_dir) if n.startswith("auditscan_")]
        self.assertEqual(leftovers, [])

    def test_03_unreachable_server_chinese_error_exit1_no_report(self):
        """验收点 7：服务不可达 -> 中文错误、退出码 1、不生成报告（旧报告也要删除）。"""
        dead_port = free_port()
        with open(self.report_path, "w", encoding="utf-8") as fh:
            fh.write('{"stale": true}')

        rc, report = self._run_auditor(dead_port)
        self.assertEqual(rc, 1)
        self.assertIsNone(report)
        self.assertFalse(os.path.isfile(self.report_path), "失败运行不得保留误导性 report.json")

    def test_04_report_required_fields_and_utf8(self):
        """验收点 3：报告字段齐全、UTF-8 编码、按风险降序。"""
        server = ServerProcess(VULN_SERVER, self.upload_dir)
        self.addCleanup(server.stop)
        self.assertTrue(server.healthy())
        _rc, report = self._run_auditor(server.port)

        for top_key in ("report_version", "generated_at", "scan_token", "target", "summary",
                        "findings", "probe_checks", "confinement", "cleanup"):
            self.assertIn(top_key, report)
        for finding in report["findings"]:
            for key in ("id", "vulnerability_type", "risk_level", "affected_paths",
                        "detected_at", "remediation"):
                self.assertIn(key, finding)
            self.assertIn(finding["risk_level"], RISK_RANK)

        # 原始字节必须是合法 UTF-8
        with open(self.report_path, "rb") as fh:
            fh.read().decode("utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
