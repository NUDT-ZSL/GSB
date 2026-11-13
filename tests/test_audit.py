# -*- coding: utf-8 -*-
"""
password_audit.py 验收测试，对应任务 9 条验收点。
运行方式（隔离模式，防止本地注入模块）：
    python -I tests/test_audit.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

# 兼容 `python -I` 隔离模式：脚本目录不在 sys.path 时手动加入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import password_audit as pa  # noqa: E402

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
MAIN_SAMPLE = os.path.join(SAMPLES_DIR, "config.json")
CORRUPTED_SAMPLE = os.path.join(SAMPLES_DIR, "config_corrupted.json")
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "password_audit.py")


class AuditTests(unittest.TestCase):
    @staticmethod
    def _run_cli(*args, cwd=None):
        # 不设置 PYTHONIOENCODING：隔离模式 (-I) 会忽略环境变量，
        # 工具自身负责把标准流重配置为 UTF-8，以此验证其自洽性
        return subprocess.run(
            [sys.executable, "-I", SCRIPT, *args],
            capture_output=True, text=True, encoding="utf-8",
            cwd=cwd,
        )

    def setUp(self):
        with open(MAIN_SAMPLE, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.report = pa.build_report(self.config)
        self.results = self.report["results"]
        self.by_name = {r["name"]: r for r in self.results}

    # 验收点 1：3~5 条合法配置生成同等数量结果，每条含 name 与风险分类
    def test_01_all_valid_entries_produce_results(self):
        self.assertEqual(len(self.config), 5)
        self.assertEqual(len(self.results), 5)
        for r in self.results:
            self.assertIn("name", r)
            self.assertIn("risk_level", r)
            self.assertIn(r["risk_level"], ("high", "medium", "low"))

    # 验收点 2：命中弱口令清单（123456）判高风险
    def test_02_weak_password_is_high_risk(self):
        self.assertEqual(self.by_name["alice"]["risk_level"], "high")
        self.assertEqual(self.by_name["alice"]["risk_category"], "weak_password")
        self.assertLessEqual(len(pa.WEAK_PASSWORDS), 20)
        self.assertIn("123456", pa.WEAK_PASSWORDS)

    # 验收点 3：纯字母 abcdef、纯数字 12345678 判中风险
    def test_03_simple_passwords_are_medium_risk(self):
        self.assertNotIn("abcdef", pa.WEAK_PASSWORDS)
        self.assertNotIn("12345678", pa.WEAK_PASSWORDS)
        self.assertEqual(self.by_name["carol"]["risk_level"], "medium")
        self.assertEqual(self.by_name["dave"]["risk_level"], "medium")
        self.assertEqual(self.by_name["carol"]["risk_category"], "simple_or_short_password")

    # 验收点 4：admin 命中高/中风险时额外标注管理员账户风险
    def test_04_admin_flag_on_admin_accounts(self):
        bob = self.by_name["bob"]
        self.assertEqual(bob["role"], "admin")
        self.assertIn(bob["risk_level"], ("high", "medium"))
        self.assertTrue(bob["admin_account_risk"])
        self.assertIn("管理员账户风险", bob["admin_risk_note"])
        # 普通用户不打管理员标注
        self.assertFalse(self.by_name["carol"]["admin_account_risk"])
        self.assertNotIn("admin_risk_note", self.by_name["carol"])

    # 验收点 5：null 口令标缺失口令，不泄露内容
    def test_05_null_password_is_missing(self):
        eve = self.by_name["eve"]
        self.assertEqual(eve["risk_category"], "missing_password")
        self.assertEqual(eve["risk_level"], "medium")
        self.assertEqual(eve["masked_password"], "***")
        self.assertNotIn("password", eve)  # 原始口令字段绝不出现在结果中
        # 缺失 password 字段（而非显式 null）同等处理
        report = pa.build_report([{"name": "nopwd", "role": "user"}])
        r = report["results"][0]
        self.assertEqual(r["risk_category"], "missing_password")
        self.assertEqual(r["masked_password"], "***")

    # 验收点 6：高风险在前；同级按 name 升序；缺失口令归入中风险
    def test_06_report_ordering(self):
        levels = [r["risk_level"] for r in self.results]
        self.assertEqual(levels, sorted(levels, key=lambda x: pa.RISK_ORDER[x]))
        # 高风险：alice(123456)、bob(admin123)，同级按 name 升序
        high = [r["name"] for r in self.results if r["risk_level"] == "high"]
        self.assertEqual(high, sorted(high))
        # 中风险：carol、dave、eve（缺失口令），同级按 name 升序
        medium = [r["name"] for r in self.results if r["risk_level"] == "medium"]
        self.assertEqual(medium, ["carol", "dave", "eve"])
        self.assertEqual(self.by_name["eve"]["risk_level"], "medium")

    # 验收点 7：缺 name/role 等损坏条目记警告并跳过，其余照常
    def test_07_corrupted_entries_warn_and_skip(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            report = pa.build_report(pa.load_config(CORRUPTED_SAMPLE))
        self.assertEqual(report["summary"]["total_entries_in_input"], 10)
        self.assertEqual(report["summary"]["entries_audited"], 6)
        self.assertEqual(report["summary"]["entries_skipped"], 4)
        self.assertEqual(len(report["warnings"]), 4)
        self.assertIn("[WARN]", buf.getvalue())
        self.assertIn("缺少必填字段 name", buf.getvalue())
        # 隔离模式下控制台输出仍为合法 UTF-8（工具自身重配置标准流）
        proc = self._run_cli(CORRUPTED_SAMPLE, "-o", os.path.join(tempfile.gettempdir(), "_enc_check.json"))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("缺少必填字段 name", proc.stderr)
        names = {r["name"] for r in report["results"]}
        self.assertNotIn("broken_role", names)
        # 跳过损坏条目后，合法条目仍完整检测
        self.assertEqual(report["results"][0]["name"], "alice")
        self.assertIn("grace", names)
        self.assertEqual(next(r for r in report["results"] if r["name"] == "grace")["risk_level"], "low")

    # 验收点 8：命令行指定输入/输出；缺省输出到当前目录 report.json
    def test_08_cli_paths_and_default_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "custom_report.json")
            proc = self._run_cli(MAIN_SAMPLE, "-o", out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.path.exists(out))
            with open(out, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data["results"]), 5)

            # 缺省 -o：在进程当前工作目录生成 report.json
            proc2 = self._run_cli(os.path.abspath(MAIN_SAMPLE), cwd=tmp)
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            self.assertTrue(os.path.exists(os.path.join(tmp, "report.json")))

    # 验收点 9：脱敏只留首尾、中间等长星号；缺失显示 ***
    def test_09_masking(self):
        self.assertEqual(pa.mask_password("123456"), "1****6")
        self.assertEqual(pa.mask_password("admin123"), "a******3")
        self.assertEqual(pa.mask_password(None), "***")
        self.assertEqual(pa.mask_password("ab"), "**")
        # 主样本报告中的实际脱敏值
        self.assertEqual(self.by_name["alice"]["masked_password"], "1****6")
        self.assertEqual(self.by_name["eve"]["masked_password"], "***")


if __name__ == "__main__":
    unittest.main(verbosity=2)
