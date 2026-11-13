# -*- coding: utf-8 -*-
"""验收测试：覆盖任务要求 1~12。

运行：python -m unittest tests.test_acceptance -v
仅使用 Python 标准库与本仓库确定性样本，不访问网络、不使用容器。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scanner.cli import main as cli_main  # noqa: E402
from scanner.detector import scan_directory  # noqa: E402
from scanner.reporter import diff_reports  # noqa: E402
from scanner.simulator import (SimConfig, simulate_all, simulate_one)  # noqa: E402

VULN_DIR = ROOT / "samples" / "vulnerable"
FIXED_DIR = ROOT / "samples" / "fixed"
SEVERITIES = {"高危", "中危", "低危"}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not VULN_DIR.exists():
            subprocess.run([sys.executable, str(ROOT / "make_samples.py")],
                           check=True, cwd=ROOT)
        cls.tmp = Path(tempfile.mkdtemp(prefix="pt_test_"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def scan_vuln(self):
        return scan_directory(str(VULN_DIR))


class T01_StaticDetection(_Base):
    """任务1：识别用户输入直接拼入写入路径的位置，含文件与行号。"""

    def test_finds_vulnerable_locations(self):
        result = self.scan_vuln()
        locations = {(f.file, f.line) for f in result.findings}
        expected = {
            ("vuln01_flask_open_concat.py", 18),
            ("vuln02_os_path_join.py", 7),
            ("vuln04_extension_only.py", 12),
            ("vuln05_pathlib_write.py", 7),
            ("vuln06_shutil_copy.py", 13),
        }
        for loc in expected:
            self.assertIn(loc, locations, f"未检出 {loc}")
        for f in result.findings:
            self.assertTrue(f.file.endswith(".py") and isinstance(f.line, int) and f.line > 0)


class T02_JsonSchema(_Base):
    """任务2：JSON 报告含 file/line/severity，severity 在三值枚举内。"""

    def test_report_fields_and_enum(self):
        result = self.scan_vuln()
        sims = simulate_all(str(VULN_DIR), result.findings, SimConfig(), str(self.tmp))
        from scanner.reporter import build_report
        report = build_report(result, sims)
        path = self.tmp / "schema.json"
        from scanner.reporter import write_report
        write_report(report, str(path))
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(loaded["vulnerabilities"]), 7)
        for v in loaded["vulnerabilities"]:
            self.assertIn("file", v)
            self.assertIn("line", v)
            self.assertIn("severity", v)
            self.assertIn(v["severity"], SEVERITIES)


class T03_ExploitSimulation(_Base):
    """任务3：含 ../ 的恶意文件名在隔离目录外产生标记文件，标记可利用。"""

    def test_marker_outside_sandbox(self):
        result = self.scan_vuln()
        sims = simulate_all(str(VULN_DIR), result.findings, SimConfig(), str(self.tmp))
        by_file = {Path(r.file).name: r for r in sims}
        r2 = by_file["vuln02_os_path_join.py"]
        self.assertTrue(r2.exploitable)
        self.assertTrue(r2.outside_upload_root)
        self.assertTrue(Path(r2.target_path).exists())
        upload_root = Path(r2.sandbox) / "uploads"
        with self.assertRaises(ValueError):
            Path(r2.target_path).resolve().relative_to(upload_root.resolve())


class T04_ConflictPreservation(_Base):
    """任务4：目标已存在同名文件时保留原文件并记录冲突。"""

    def test_original_preserved_and_conflict_recorded(self):
        result = self.scan_vuln()
        target = next(f for f in result.findings
                      if f.file == "vuln02_os_path_join.py")
        cfg = SimConfig(precreate_conflict=True)
        r = simulate_one(str(VULN_DIR), target, cfg, str(self.tmp))
        self.assertIsNotNone(r.conflict)
        self.assertFalse(r.conflict["overwritten"])
        self.assertTrue(r.conflict["content_preserved"])
        self.assertEqual(
            Path(r.target_path).read_bytes(),
            b"ORIGINAL-CONTENT-MUST-BE-PRESERVED\n",
        )


class T05_SuggestionActionable(_Base):
    """任务5：每条漏洞 suggestion 含具体函数名/校验逻辑。"""

    def test_suggestions_contain_functions(self):
        result = self.scan_vuln()
        keywords = ("basename", "commonpath", "is_relative_to",
                    "secure_filename", "uuid", "resolve", "realpath")
        for f in result.findings:
            self.assertTrue(any(k in f.suggestion for k in keywords), f.id)


class T06_FixedRescanComparison(_Base):
    """任务6：修复后重扫，摘要含 fixed_count 与 remaining_count，数值一致。"""

    def test_comparison_counts(self):
        base = scan_directory(str(VULN_DIR))
        fixed = scan_directory(str(FIXED_DIR))
        comp = diff_reports(base, fixed)
        self.assertEqual(comp["fixed_count"], len(base.findings))
        self.assertEqual(comp["remaining_count"], 0)
        self.assertEqual(comp["new_count"], 0)
        self.assertEqual(len(comp["per_vulnerability"]), len(base.findings))
        self.assertTrue(all(row["status"] == "已修复"
                            for row in comp["per_vulnerability"]))


class T07_SyntaxErrorSkipped(_Base):
    """任务7：语法错误文件跳过并记入 errors，不中断其他文件检测。"""

    def test_error_recorded_others_scanned(self):
        result = self.scan_vuln()
        err_files = {e["file"] for e in result.errors}
        self.assertIn("vuln08_broken_syntax.py", err_files)
        detected = {f.file for f in result.findings}
        self.assertNotIn("vuln08_broken_syntax.py", detected)
        self.assertGreaterEqual(len(detected), 7)


class T08_ByteExactContent(_Base):
    """任务8：指定上传内容后，目标文件内容逐字节一致。"""

    def test_content_matches_byte_for_byte(self):
        payload = b"\x00\x01\x02BINARY-\xe4\xb8\xad\xe6\x96\x87-END\n"
        result = self.scan_vuln()
        target = next(f for f in result.findings
                      if f.file == "vuln05_pathlib_write.py")
        cfg = SimConfig(content=payload)
        r = simulate_one(str(VULN_DIR), target, cfg, str(self.tmp))
        self.assertTrue(r.content_matches)
        self.assertEqual(Path(r.target_path).read_bytes(), payload)


class T09_CommandLineDefaults(_Base):
    """任务9：无参用默认值；带参按指定路径生成报告。"""

    def test_cli_with_args_and_defaults(self):
        out1 = self.tmp / "cli_arg.json"
        code = cli_main(["--dir", str(VULN_DIR), "--no-sim", "--report", str(out1)])
        self.assertEqual(code, 0)
        self.assertTrue(out1.exists())
        # 无参（默认目录=当前目录，默认报告 out/report.json）
        cwd = self.tmp / "cwd_default"
        cwd.mkdir()
        shutil.copytree(VULN_DIR, cwd / "src",
                        ignore=shutil.ignore_patterns("__pycache__"))
        old_cwd = os.getcwd()
        try:
            os.chdir(cwd / "src")
            code = cli_main(["--no-sim"])
            self.assertEqual(code, 0)
            self.assertTrue((cwd / "src" / "out" / "report.json").exists())
        finally:
            os.chdir(old_cwd)


class T10_ReadOnlyFailure(_Base):
    """任务10：只读目录模拟输出权限提示、退出码非零且不崩溃。"""

    def test_readonly_returns_nonzero(self):
        out = self.tmp / "readonly.json"
        code = cli_main(["--dir", str(VULN_DIR), "--read-only",
                         "--report", str(out)])
        self.assertEqual(code, 2)
        report = json.loads(out.read_text(encoding="utf-8"))
        errors = [v["simulation"]["error"] for v in report["vulnerabilities"]
                  if v["simulation"].get("error")]
        self.assertTrue(any("权限不足" in e for e in errors))


class T11_DeterministicOrder(_Base):
    """任务11：severity 降序、同级 file 字母序，两次运行顺序一致。"""

    def test_order_stable(self):
        from scanner.reporter import build_report
        r1 = build_report(scan_directory(str(VULN_DIR)), None)
        r2 = build_report(scan_directory(str(VULN_DIR)), None)
        keys = [(v["severity"], v["file"], v["line"]) for v in r1["vulnerabilities"]]
        self.assertEqual(keys, [(v["severity"], v["file"], v["line"])
                                for v in r2["vulnerabilities"]])
        order = [{"高危": 0, "中危": 1, "低危": 2}[s] for s, _, _ in keys]
        self.assertEqual(order, sorted(order))
        # 同级按 file 字母序
        from itertools import groupby
        for sev, group in groupby(keys, key=lambda k: k[0]):
            files = [g[1] for g in group]
            self.assertEqual(files, sorted(files))


class T12_BeforeAfterTextSummary(_Base):
    """任务12：修复前后文本摘要逐条列出编号与状态，与对比数据一致。"""

    def test_text_summary_lists_status(self):
        from scanner.reporter import (build_report, diff_reports,
                                       render_text_summary)
        base = scan_directory(str(VULN_DIR))
        fixed = scan_directory(str(FIXED_DIR))
        comp = diff_reports(base, fixed)
        report = build_report(fixed, None, comparison=comp)
        text = render_text_summary(report)
        for row in comp["per_vulnerability"]:
            self.assertIn(row["id"], text)
            self.assertIn(row["status"], text)
        self.assertIn(f"fixed_count     = {comp['fixed_count']}", text)
        self.assertIn(f"remaining_count = {comp['remaining_count']}", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
