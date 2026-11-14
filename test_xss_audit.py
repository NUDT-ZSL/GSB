"""xss_audit 验收测试（纯标准库 unittest，覆盖任务 14 条验收点）。

运行：
    python -m unittest -v test_xss_audit
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

import build_samples
import xss_audit

ROOT = os.path.dirname(os.path.abspath(__file__))


class AuditTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sample_dir = os.path.join(self.tmp.name, "samples")
        self.report_path = os.path.join(self.tmp.name, "out", "report.json")
        build_samples.build(self.sample_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_audit(self) -> dict:
        rc = xss_audit.main([self.sample_dir, self.report_path])
        self.assertEqual(rc, 0)
        with open(self.report_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def by_id(self, report: dict) -> dict:
        return {c["case_id"]: c for c in report["cases"]}


class TestCliAndReport(AuditTestBase):
    # 验收1：缺参数时非零退出且有提示
    def test_01_missing_args_nonzero(self) -> None:
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "xss_audit.py")],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(proc.stderr.strip())

        proc_one = subprocess.run(
            [sys.executable, os.path.join(ROOT, "xss_audit.py"), self.sample_dir],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc_one.returncode, 0)

    # 验收1：带两参数运行后报告文件存在
    def test_01_report_file_created(self) -> None:
        self.run_audit()
        self.assertTrue(os.path.isfile(self.report_path))

    # 验收8：报告可被 json 解析且字段含义稳定
    def test_08_report_schema(self) -> None:
        report = self.run_audit()
        self.assertEqual(
            set(report.keys()), {"summary", "notices", "problems", "cases"}
        )
        expected_fields = {
            "case_id", "valid", "vulnerable", "matched_rules",
            "fix_conclusion", "raw_rendered", "fixed_rendered", "error",
        }
        for case in report["cases"]:
            self.assertEqual(set(case.keys()), expected_fields)

    # 验收9：两次运行顺序完全相同且按标识升序
    def test_09_deterministic_order(self) -> None:
        first = self.run_audit()
        ids_first = [c["case_id"] for c in first["cases"]]
        second = self.run_audit()
        ids_second = [c["case_id"] for c in second["cases"]]
        self.assertEqual(ids_first, ids_second)
        self.assertEqual(ids_first, sorted(ids_first))

    # 验收11：空目录 -> 空明细、提示、退出码 0
    def test_11_empty_dir(self) -> None:
        empty = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty)
        out = os.path.join(self.tmp.name, "empty_report.json")
        rc = xss_audit.main([empty, out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report["cases"], [])
        self.assertTrue(report["notices"])

    # 验收11：不存在的目录同样不崩溃
    def test_11_missing_dir(self) -> None:
        out = os.path.join(self.tmp.name, "missing_report.json")
        rc = xss_audit.main([os.path.join(self.tmp.name, "no_such_dir"), out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report["cases"], [])
        self.assertTrue(report["notices"])

    # 验收12：报告不含绝对路径，用例标识仅为文件名
    def test_12_no_absolute_path_leak(self) -> None:
        report = self.run_audit()
        raw = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(self.tmp.name, raw)
        self.assertNotIn(":\\", raw)  # 无盘符
        for case in report["cases"]:
            self.assertNotIn(os.sep, case["case_id"])
            self.assertEqual(case["case_id"], os.path.basename(case["case_id"]))
        for problem in report["problems"]:
            self.assertNotIn(os.sep, problem["file"])


class TestCases(AuditTestBase):
    # 验收2：缺字段/类型不符 -> 不合法且不参与漏洞判定
    def test_02_invalid_cases(self) -> None:
        report = self.run_audit()
        cases = self.by_id(report)
        for cid in ("07_missing_field.json", "08_wrong_type.json"):
            case = cases[cid]
            self.assertFalse(case["valid"])
            self.assertFalse(case["vulnerable"])
            self.assertEqual(case["fix_conclusion"], "不合法用例")
            self.assertIsNone(case["raw_rendered"])
            self.assertIsNone(case["fixed_rendered"])
            self.assertTrue(case["error"])

    # 验收3：合法用例同时含未修复与修复两段渲染文本
    def test_03_both_render_texts(self) -> None:
        report = self.run_audit()
        for case in report["cases"]:
            if case["valid"] and case["fix_conclusion"] != "处理失败":
                self.assertIsInstance(case["raw_rendered"], str)
                self.assertIsInstance(case["fixed_rendered"], str)

    # 验收4：未修复模式原样保留尖括号与引号
    def test_04_raw_keeps_special_chars(self) -> None:
        report = self.run_audit()
        case = self.by_id(report)["01_script_basic.json"]
        self.assertIn("<script>alert('xss')</script>", case["raw_rendered"])
        case2 = self.by_id(report)["02_event_attr.json"]
        self.assertIn('onerror="alert(1)"', case2["raw_rendered"])

    # 验收5：修复模式尖括号/引号变为实体，原始标签片段消失
    def test_05_escaped_output(self) -> None:
        report = self.run_audit()
        case = self.by_id(report)["01_script_basic.json"]
        fixed = case["fixed_rendered"]
        self.assertIn("&lt;script&gt;", fixed)
        self.assertIn("&#x27;", fixed)  # 单引号被转义
        self.assertNotIn("<script>", fixed)
        case2 = self.by_id(report)["02_event_attr.json"]
        self.assertIn("&quot;", case2["fixed_rendered"])
        self.assertNotIn('onerror="alert(1)"', case2["fixed_rendered"])

    # 验收6：script 标签判漏洞，纯文本判无漏洞
    def test_06_detection(self) -> None:
        report = self.run_audit()
        cases = self.by_id(report)
        for cid in (
            "01_script_basic.json",
            "02_event_attr.json",
            "03_event_attr_single_quote.json",
            "04_onload_attr.json",
        ):
            self.assertTrue(cases[cid]["vulnerable"], cid)
            self.assertTrue(cases[cid]["matched_rules"])
        for cid in ("05_plain_text.json", "06_benign_brackets_and_number.json"):
            self.assertFalse(cases[cid]["vulnerable"], cid)
            self.assertEqual(cases[cid]["matched_rules"], [])

    # 验收7：漏洞用例修复后不再命中规则，结论为已修复
    def test_07_fixed_conclusion(self) -> None:
        report = self.run_audit()
        for case in report["cases"]:
            if case["vulnerable"]:
                self.assertEqual(case["fix_conclusion"], "已修复")
                self.assertFalse(xss_audit.detect_xss(case["fixed_rendered"])["vulnerable"])
        safe = self.by_id(report)["05_plain_text.json"]
        self.assertEqual(safe["fix_conclusion"], "无漏洞无需修复")

    # 验收10：渲染异常标记失败并附原因，其余用例继续
    def test_10_render_exception_isolated(self) -> None:
        report = self.run_audit()
        cases = self.by_id(report)
        failed = cases["09_render_exception.json"]
        self.assertEqual(failed["fix_conclusion"], "处理失败")
        self.assertIn("KeyError", failed["error"])
        # 其余用例仍正常出结果
        self.assertTrue(cases["01_script_basic.json"]["raw_rendered"])
        self.assertTrue(cases["05_plain_text.json"]["raw_rendered"])

    # 验收13：汇总计数与明细逐条统计一致
    def test_13_summary_matches_details(self) -> None:
        report = self.run_audit()
        cases = report["cases"]
        valid = [c for c in cases if c["valid"]]
        vuln = [c for c in valid if c["vulnerable"]]
        fixed = [c for c in vuln if c["fix_conclusion"] == "已修复"]
        s = report["summary"]
        self.assertEqual(s["valid_case_count"], len(valid))
        self.assertEqual(s["vulnerable_case_count"], len(vuln))
        self.assertEqual(s["fixed_case_count"], len(fixed))
        self.assertEqual(
            s["invalid_case_count"], len(cases) - len(valid)
        )
        # 确定性样本的固定数值
        self.assertEqual(s["valid_case_count"], 7)
        self.assertEqual(s["vulnerable_case_count"], 4)
        self.assertEqual(s["fixed_case_count"], 4)
        self.assertEqual(s["invalid_case_count"], 3)  # 2 不合法 + 1 失败

    # 验收14：不可读文件进入问题列表且不中断整体处理
    def test_14_unreadable_file_listed(self) -> None:
        report = self.run_audit()
        files = {p["file"] for p in report["problems"]}
        self.assertIn("10_unreadable_broken.json", files)
        # 其余用例仍完成
        self.assertEqual(
            report["summary"]["total_records"],
            len(build_samples.SAMPLES) + 1,
        )
        self.assertTrue(self.by_id(report)["01_script_basic.json"]["vulnerable"])


class TestUnitHelpers(unittest.TestCase):
    def test_detect_xss_unit(self) -> None:
        self.assertTrue(xss_audit.detect_xss("<script>x</script>")["vulnerable"])
        self.assertTrue(
            xss_audit.detect_xss("<img src=x onerror=alert(1)>")["vulnerable"]
        )
        self.assertTrue(
            xss_audit.detect_xss('<img src=x onerror="alert(1)">')["vulnerable"]
        )
        self.assertFalse(xss_audit.detect_xss("普通文本 1 < 2 且 3 > 2")["vulnerable"])
        # 转义后的内容不命中（script 标签、事件属性两种形态）
        escaped = xss_audit.html.escape("<script>x</script>", quote=True)
        self.assertFalse(xss_audit.detect_xss(escaped)["vulnerable"])
        escaped_event = xss_audit.html.escape(
            "<img src=\"x\" onerror=\"alert(1)\">", quote=True
        )
        self.assertFalse(xss_audit.detect_xss(escaped_event)["vulnerable"])

    def test_validate_unit(self) -> None:
        ok, _ = xss_audit.validate_case({"template": "a{{x}}", "data": {"x": 1}})
        self.assertTrue(ok)
        self.assertFalse(xss_audit.validate_case({"template": "a"})[0])
        self.assertFalse(xss_audit.validate_case({"template": 1, "data": {}})[0])
        self.assertFalse(xss_audit.validate_case(["not", "dict"])[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
