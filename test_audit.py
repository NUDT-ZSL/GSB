#!/usr/bin/env python3
"""audit_tool 自动化测试：覆盖任务验收点 1-13。

运行: python test_audit.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "audit_tool.py")
RULES = os.path.join(ROOT, "rules.json")


def write(path, content, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if "b" in mode:
        with open(path, mode) as f:
            f.write(content)
    else:
        with open(path, mode, encoding="utf-8", newline="\n") as f:
            f.write(content)


def run_tool(audit_dir, rules, out, extra=None):
    cmd = [sys.executable, TOOL, audit_dir, rules, out] + (extra or [])
    return subprocess.run(cmd, capture_output=True, text=True)


class AuditToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_test_")
        self.src = os.path.join(self.tmp, "src")
        self.out = os.path.join(self.tmp, "report.json")
        os.makedirs(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load_report(self):
        with open(self.out, encoding="utf-8") as f:
            return json.load(f)

    # 验收1+10: 报告存在、合法 JSON、含 summary/findings/warnings 且类型正确
    def test_01_report_structure(self):
        write(os.path.join(self.src, "a.py"), "eval('1')\n")
        r = run_tool(self.src, RULES, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(self.out))
        rep = self.load_report()
        self.assertIn("summary", rep)
        self.assertIn("findings", rep)
        self.assertIn("warnings", rep)
        self.assertIsInstance(rep["summary"]["files_audited"], int)
        self.assertIsInstance(rep["summary"]["findings_count"], int)
        self.assertIsInstance(rep["findings"], list)
        self.assertIsInstance(rep["warnings"], list)
        self.assertTrue(all(isinstance(w, str) for w in rep["warnings"]))

    # 验收2: 缺字段规则不生效且有告警；合法规则正常加载
    def test_02_rule_missing_fields(self):
        bad_rules = [
            {"id": "OK1", "pattern": "eval\\(", "severity": "高", "fix": "f"},
            {"id": "NOFIX", "pattern": "eval\\(", "severity": "高"},
            {"pattern": "eval\\(", "severity": "高", "fix": "f"},
            {"id": "NOPAT", "severity": "高", "fix": "f"},
        ]
        rp = os.path.join(self.tmp, "r.json")
        write(rp, json.dumps(bad_rules, ensure_ascii=False))
        write(os.path.join(self.src, "a.py"), "eval('1')\n")
        r = run_tool(self.src, rp, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = self.load_report()
        self.assertEqual(len(rep["findings"]), 1)
        self.assertEqual(rep["findings"][0]["rule_id"], "OK1")
        self.assertEqual(len(rep["warnings"]), 3)
        self.assertTrue(any("NOFIX" in w for w in rep["warnings"]))

    # 验收3: 仅审计 .py 文件
    def test_03_only_py_files(self):
        write(os.path.join(self.src, "a.py"), "eval('1')\n")
        write(os.path.join(self.src, "b.txt"), "eval('1')\n")
        write(os.path.join(self.src, "c.md"), "eval('1')\n")
        r = run_tool(self.src, RULES, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = self.load_report()
        files = {f["file"] for f in rep["findings"]}
        self.assertEqual(files, {"a.py"})
        self.assertEqual(rep["summary"]["files_audited"], 1)

    # 验收4: 文件名/行号/列号(从1开始)/规则id/severity 正确
    def test_04_location_fields(self):
        write(os.path.join(self.src, "a.py"),
              "x = 1\ny = 2\n    eval('1')\n")
        r = run_tool(self.src, RULES, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = self.load_report()
        self.assertEqual(len(rep["findings"]), 1)
        f = rep["findings"][0]
        self.assertEqual(f["file"], "a.py")
        self.assertEqual(f["line"], 3)
        self.assertEqual(f["column"], 5)
        self.assertEqual(f["rule_id"], "SEC001")
        self.assertEqual(f["severity"], "高")
        self.assertIn("fix", f)

    # 验收5: 同一行多次匹配独立成条且列号不同
    def test_05_multiple_matches_same_line(self):
        write(os.path.join(self.src, "a.py"), "eval(a) + eval(b)\n")
        r = run_tool(self.src, RULES, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = self.load_report()
        fs = [f for f in rep["findings"] if f["rule_id"] == "SEC001"]
        self.assertEqual(len(fs), 2)
        self.assertEqual(fs[0]["line"], fs[1]["line"])
        self.assertNotEqual(fs[0]["column"], fs[1]["column"])
        self.assertEqual((fs[0]["column"], fs[1]["column"]), (1, 11))

    # 验收6: 按 文件名->行号->列号 升序排序
    def test_06_sorting(self):
        write(os.path.join(self.src, "b.py"), "eval(1)\neval(2)\n")
        write(os.path.join(self.src, "a.py"), "x\neval(1)\neval(1) eval(2)\n")
        r = run_tool(self.src, RULES, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = self.load_report()
        keys = [(f["file"], f["line"], f["column"]) for f in rep["findings"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(keys, [("a.py", 2, 1), ("a.py", 3, 1),
                                ("a.py", 3, 9), ("b.py", 1, 1),
                                ("b.py", 2, 1)])

    # 验收7: severity 非法 -> 规则不生效+告警，其余规则照常
    def test_07_invalid_severity(self):
        bad_rules = [
            {"id": "BADSEV", "pattern": "eval\\(", "severity": "严重", "fix": "f"},
            {"id": "OK2", "pattern": "eval\\(", "severity": "中", "fix": "f"},
        ]
        rp = os.path.join(self.tmp, "r.json")
        write(rp, json.dumps(bad_rules, ensure_ascii=False))
        write(os.path.join(self.src, "a.py"), "eval('1')\n")
        r = run_tool(self.src, rp, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = self.load_report()
        self.assertEqual(len(rep["findings"]), 1)
        self.assertEqual(rep["findings"][0]["rule_id"], "OK2")
        self.assertTrue(any("BADSEV" in w and "severity" in w
                            for w in rep["warnings"]))

    # 验收8: 正则无效 -> 规则不生效+告警，其余规则照常
    def test_08_invalid_regex(self):
        bad_rules = [
            {"id": "BADRE", "pattern": "eval([", "severity": "高", "fix": "f"},
            {"id": "OK3", "pattern": "eval\\(", "severity": "高", "fix": "f"},
        ]
        rp = os.path.join(self.tmp, "r.json")
        write(rp, json.dumps(bad_rules, ensure_ascii=False))
        write(os.path.join(self.src, "a.py"), "eval('1')\n")
        r = run_tool(self.src, rp, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = self.load_report()
        self.assertEqual(len(rep["findings"]), 1)
        self.assertEqual(rep["findings"][0]["rule_id"], "OK3")
        self.assertTrue(any("BADRE" in w and "正则" in w
                            for w in rep["warnings"]))

    # 验收9: 无法读取的 .py 文件被跳过并告警，其余文件照常
    def test_09_unreadable_file(self):
        write(os.path.join(self.src, "good.py"), "eval('1')\n")
        # 写入非法 UTF-8 字节，使读取必然失败（跨平台确定性）
        write(os.path.join(self.src, "bad.py"), b"\xff\xfe\x00eval(", mode="wb")
        r = run_tool(self.src, RULES, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = self.load_report()
        self.assertEqual(rep["summary"]["files_audited"], 1)
        self.assertEqual(len(rep["findings"]), 1)
        self.assertEqual(rep["findings"][0]["file"], "good.py")
        self.assertTrue(any("bad.py" in w and "读取失败" in w
                            for w in rep["warnings"]))

    # 验收11: --exclude 排除目录或文件
    def test_11_exclude(self):
        write(os.path.join(self.src, "keep.py"), "eval('1')\n")
        write(os.path.join(self.src, "skipme", "x.py"), "eval('2')\n")
        write(os.path.join(self.src, "one.py"), "eval('3')\n")
        r = run_tool(self.src, RULES, self.out,
                     extra=["--exclude", "skipme,one.py"])
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = self.load_report()
        files = {f["file"] for f in rep["findings"]}
        self.assertEqual(files, {"keep.py"})
        self.assertEqual(rep["summary"]["files_audited"], 1)

    # 验收12: 无缺陷 -> findings 空、缺陷数 0、退出码 0
    def test_12_clean_and_empty(self):
        write(os.path.join(self.src, "clean.py"), "print('hello')\n")
        r = run_tool(self.src, RULES, self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = self.load_report()
        self.assertEqual(rep["findings"], [])
        self.assertEqual(rep["summary"]["findings_count"], 0)
        # 空目录
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        r2 = run_tool(empty, RULES, self.out)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        rep2 = self.load_report()
        self.assertEqual(rep2["findings"], [])
        self.assertEqual(rep2["summary"]["files_audited"], 0)

    # 验收13: 目录不存在 / 规则文件缺失 -> 非0退出 + stderr 提示
    def test_13_missing_inputs(self):
        r = run_tool(os.path.join(self.tmp, "no_such_dir"), RULES, self.out)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(r.stderr.strip())
        r2 = run_tool(self.src, os.path.join(self.tmp, "no_rules.json"), self.out)
        self.assertNotEqual(r2.returncode, 0)
        self.assertTrue(r2.stderr.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
