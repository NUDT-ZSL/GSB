"""pysentry 验收测试：逐条对应任务的 12 项验收要求。

运行（项目根目录）：
  python -I tests\test_acceptance.py
  或  python -m unittest -v
"""
from __future__ import annotations

import ast
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

# 直接以脚本方式运行（python -I tests\test_acceptance.py）时补包路径
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pysentry.compare import compare_scans  # noqa: E402
from pysentry.rules import (  # noqa: E402
    check_path_open,
    check_recursion,
    check_sensitive_output,
)
from pysentry.samples import build_sample_tree  # noqa: E402
from pysentry.scanner import build_report, scan_directory  # noqa: E402


class AcceptanceTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="pysentry_test_")
        cls.base = pathlib.Path(cls._tmp.name)
        build_sample_tree(cls.base)
        cls.before = cls.base / "before"
        cls.after = cls.base / "after"
        cls.syntax_dir = cls.base / "syntax_err"
        cls.empty_dir = cls.base / "empty"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _rules(self, findings, rule_id: str) -> list[tuple[str, int]]:
        return [(f.file, f.line) for f in findings if f.rule_id == rule_id]

    def scan(self, sub: str):
        return scan_directory(self.base / sub)


class TestAcceptance(AcceptanceTestBase):
    # 验收 1：三个 .py 文件的目录，报告恰好覆盖三个文件且无遗漏
    def test_01_covers_all_three_files(self):
        findings, skipped, scanned = self.scan("before")
        self.assertEqual(
            sorted(scanned),
            ["path_open_demo.py", "recursion_demo.py", "secret_leak.py"],
        )
        self.assertEqual(skipped, [])
        touched = {f.file for f in findings}
        self.assertEqual(touched, set(scanned))

    # 验收 2：路径拼接后直接打开被检出，行号与源码一致
    def test_02_path_traversal_detected_with_correct_line(self):
        findings, _, _ = self.scan("before")
        hits = self._rules(findings, "SEC001")
        self.assertEqual(hits, [("path_open_demo.py", 8)])
        source = (self.before / "path_open_demo.py").read_text(encoding="utf-8").splitlines()
        self.assertIn("open(path)", source[7])

    # 验收 3：无终止递归被检出，含终止条件不检出
    def test_03_recursion_detection(self):
        findings, _, _ = self.scan("before")
        self.assertEqual(self._rules(findings, "SEC002"), [("recursion_demo.py", 4)])
        after_findings, _, _ = self.scan("after")
        self.assertEqual(self._rules(after_findings, "SEC002"), [])

    # 验收 4：直接输出令牌变量被检出，固定文案不检出
    def test_04_secret_leak_detection(self):
        findings, _, _ = self.scan("before")
        secret_lines = sorted(line for file, line in self._rules(findings, "SEC003"))
        # 第 10、11 行各一条，第 13 行两个敏感变量各一条 -> 共 4 条，无第 12 行（固定文案）
        self.assertEqual(secret_lines, [10, 11, 13, 13])

    # 验收 5：四字段齐全，路径为相对路径
    def test_05_finding_has_four_fields_and_relative_path(self):
        report = build_report(self.before)
        for item in report["issues"]:
            self.assertEqual(
                sorted(item.keys()),
                ["file", "line", "message", "rule_id"],
            )
            self.assertNotIn(":", item["file"])
            self.assertNotIn("\\", item["file"])
            self.assertIsInstance(item["line"], int)
            self.assertTrue(item["message"])

    # 验收 6：两次运行字节一致，排序为路径字典序 + 行号升序
    def test_06_deterministic_and_sorted(self):
        payloads = []
        for _ in range(2):
            report = build_report(self.before)
            payloads.append(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
        self.assertEqual(payloads[0], payloads[1])
        issues = json.loads(payloads[0].decode("utf-8"))["issues"]
        keys = [(i["file"], i["line"], i["rule_id"]) for i in issues]
        self.assertEqual(keys, sorted(keys))

    # 验收 7：空目录/不存在目录输出空列表并给出提示
    def test_07_empty_and_missing_directory(self):
        report = build_report(self.empty_dir)
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["issue_count"], 0)
        self.assertIn("未发现可扫描目标", report["notice"])

        missing = build_report(self.base / "does_not_exist_xyz")
        self.assertEqual(missing["issues"], [])
        self.assertIn("notice", missing)

    # 验收 8：语法错误文件被标注跳过，好文件照常输出
    def test_08_syntax_error_isolated(self):
        report = build_report(self.syntax_dir)
        skipped_files = [s["file"] for s in report["skipped_files"]]
        self.assertEqual(skipped_files, ["broken_syntax.py"])
        self.assertEqual(report["scanned_files"], ["good_one.py"])
        self.assertEqual(
            [(i["file"], i["rule_id"]) for i in report["issues"]],
            [("good_one.py", "SEC003")],
        )

    # 验收 9：修复前后对比，已修复问题正确归类
    def test_09_compare_fixed_classification(self):
        result = compare_scans(self.before, self.after)
        fixed = {(i["file"], i["rule_id"]) for i in result["fixed_issues"]}
        self.assertIn(("path_open_demo.py", "SEC001"), fixed)
        self.assertIn(("recursion_demo.py", "SEC002"), fixed)
        self.assertEqual(result["fixed_count"], 2)
        self.assertNotIn(("secret_leak.py", "SEC003"), fixed)

    # 验收 10：相对路径+规则标识匹配，行号后移仍归为仍存在
    def test_10_line_shift_still_remaining(self):
        result = compare_scans(self.before, self.after)
        remaining = result["remaining_issues"]
        self.assertEqual(len(remaining), 4)
        self.assertTrue(all(i["rule_id"] == "SEC003" for i in remaining))
        # 修复后新增一行注释，行号整体 +1：10->11, 11->12, 13->14（两条）
        self.assertEqual(sorted(i["line"] for i in remaining), [11, 12, 14, 14])
        self.assertEqual(result["introduced_issues"], [])

    # 验收 11：输出文件为合法 UTF-8 JSON，控制台计数与文件一致
    def test_11_output_file_and_console_count(self):
        out_file = self.base / "report.json"
        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / "run_scanner.py"),
             "scan", str(self.before), "-o", str(out_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        raw = out_file.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))  # 合法 JSON 且为 UTF-8
        self.assertEqual(parsed["issue_count"], len(parsed["issues"]))
        self.assertIn(f"问题总数: {parsed['issue_count']}", proc.stdout)

        # 对比命令同样支持文件输出
        diff_file = self.base / "diff.json"
        subprocess.run(
            [sys.executable, "-I", str(ROOT / "run_scanner.py"),
             "compare", str(self.before), str(self.after), "-o", str(diff_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        diff = json.loads(diff_file.read_bytes().decode("utf-8"))
        self.assertEqual(diff["fixed_count"], 2)
        self.assertEqual(diff["remaining_count"], 4)

    # 验收 12：同行两个敏感变量分别报告，不去重合并
    def test_12_duplicate_same_rule_reported_separately(self):
        findings, _, _ = self.scan("before")
        line_13 = [f for f in findings if f.rule_id == "SEC003" and f.line == 13]
        self.assertEqual(len(line_13), 2)


class TestRuleDetails(unittest.TestCase):
    """规则细节补充测试：污点传播、白名单安全查表、良性递归、固定文案。"""

    def _tree(self, source: str) -> ast.AST:
        return ast.parse(source)

    def test_os_path_join_propagates_taint(self):
        source = (
            "import os\n"
            "def f(base):\n"
            "    n = input()\n"
            "    p = os.path.join(base, n)\n"
            "    return open(p)\n"
        )
        hits = check_path_open(self._tree(source), "x.py")
        self.assertEqual([(h.rule_id, h.line) for h in hits], [("SEC001", 5)])

    def test_argv_concat_detected(self):
        source = (
            "import sys\n"
            "p = '/data/' + sys.argv[1]\n"
            "open(p)\n"
        )
        hits = check_path_open(self._tree(source), "x.py")
        self.assertEqual(len(hits), 1)

    def test_allowlist_lookup_not_tainted(self):
        source = (
            "ALLOWED = {'a': 'safe/a.txt'}\n"
            "def f():\n"
            "    name = input()\n"
            "    path = ALLOWED[name]\n"
            "    return open(path)\n"
        )
        self.assertEqual(check_path_open(self._tree(source), "x.py"), [])

    def test_constant_open_not_flagged(self):
        source = "open('config/default.txt')\n"
        self.assertEqual(check_path_open(self._tree(source), "x.py"), [])

    def test_recursion_with_base_case_not_flagged(self):
        source = (
            "def expand(items):\n"
            "    if not items:\n"
            "        return []\n"
            "    return expand(items[1:])\n"
        )
        self.assertEqual(check_recursion(self._tree(source), "x.py"), [])

    def test_recursion_raise_guard_not_flagged(self):
        source = (
            "def expand(depth):\n"
            "    if depth > 100:\n"
            "        raise RuntimeError('too deep')\n"
            "    return expand(depth + 1)\n"
        )
        self.assertEqual(check_recursion(self._tree(source), "x.py"), [])

    def test_non_recursive_function_not_flagged(self):
        source = (
            "def f():\n"
            "    return 42\n"
        )
        self.assertEqual(check_recursion(self._tree(source), "x.py"), [])

    def test_fixed_text_output_not_flagged(self):
        source = (
            "def run(token_value):\n"
            "    print('job started')\n"
            "    print('done without secrets')\n"
        )
        self.assertEqual(check_sensitive_output(self._tree(source), "x.py"), [])

    def test_logger_and_fstring_flagged(self):
        source = (
            "import logging\n"
            "logger = logging.getLogger()\n"
            "api_token = 't'\n"
            "def f():\n"
            "    logger.warning(f'x={api_token}')\n"
        )
        hits = check_sensitive_output(self._tree(source), "x.py")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].line, 5)


if __name__ == "__main__":
    # 详细输出，便于演示时逐条核对验收项
    unittest.main(verbosity=2)
