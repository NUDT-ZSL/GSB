"""12 项验收要求的完整测试。

每个 test_ 方法的 docstring 标注其覆盖的验收条目编号。
测试通过确定性样本生成器在临时目录中搭建环境，不访问网络。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scanner.compare import compare_reports
from scanner.detector import detect
from scanner.engine import scan
from scanner.models import (
    CONCLUSION_CONFIRMED,
    CONCLUSION_SUSPECTED,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    SEVERITY_ORDER,
)
from scanner.report_io import read_report, write_report
from scanner.sample_generator import generate


class ScannerAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls._tmp.name)
        cls.scan_root = cls.workspace / "workdir"
        generate(cls.scan_root)
        cls.fixed_root = cls.scan_root / "fixed_samples"
        cls.report_path = cls.workspace / "report_before.json"
        write_report(scan(cls.scan_root), cls.report_path)
        cls.report = read_report(cls.report_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _sample(self, sample_id: str) -> dict:
        for sample in self.report["samples"]:
            if sample["sample_id"] == sample_id:
                return sample
        self.fail(f"报告中缺少样本 {sample_id}")

    # 验收 1：声明纳入检测的样本逐条覆盖、不遗漏
    def test_01_all_declared_samples_covered(self):
        """验收1：六个纳入样本（含缺失的 S06）在报告中各有一条记录，排除项 S07 不出现。"""

        ids = [s["sample_id"] for s in self.report["samples"]]
        self.assertEqual(ids, ["S01", "S02", "S03", "S04", "S05", "S06"])
        self.assertNotIn("S07", ids)
        self.assertEqual(self.report["summary"]["declared_samples"], 6)

    # 验收 2：识别响应头中的服务标识与版本线索并列出头名称
    def test_02_server_version_header_flagged_with_header_name(self):
        """验收2：S01 的 Server 版本头被标记，field_name 明确给出 Server。"""

        s01 = self._sample("S01")
        header_findings = [f for f in s01["findings"] if f["location"].startswith("响应头")]
        server_findings = [f for f in header_findings if f["field_name"] == "Server"]
        self.assertEqual(len(server_findings), 1)
        self.assertEqual(server_findings[0]["severity"], SEVERITY_MEDIUM)
        self.assertEqual(server_findings[0]["conclusion"], CONCLUSION_CONFIRMED)
        # 另一个标识头 X-Powered-By 同样被指认头名
        powered = [f for f in header_findings if f["field_name"] == "X-Powered-By"]
        self.assertEqual(len(powered), 1)

    # 验收 3：响应正文明文口令字段，高危且指出字段
    def test_03_plaintext_password_high_risk_with_field(self):
        """验收3：S02 的 password 字段被标为高危，field_name 指向 password。"""

        s02 = self._sample("S02")
        passwords = [f for f in s02["findings"] if f["field_name"] == "password"]
        self.assertEqual(len(passwords), 1)
        self.assertEqual(passwords[0]["severity"], SEVERITY_HIGH)
        self.assertEqual(passwords[0]["conclusion"], CONCLUSION_CONFIRMED)
        self.assertIn("password", passwords[0]["location"])

    # 验收 4：修复后重新比对，对应暴露状态为已消除
    def test_04_fixed_sample_marks_exposure_removed(self):
        """验收4：修复版报告中 S01/S02/S04 的原问题消失，比对结果为已消除。"""

        fixed_report_path = self.workspace / "report_fixed.json"
        write_report(scan(self.fixed_root), fixed_report_path)
        result = compare_reports(self.report_path, fixed_report_path)

        removed = {(e["sample_id"], e["category"]) for e in result["entries"] if e["status"] == "已消除"}
        self.assertIn(("S01", "响应头服务版本暴露"), removed)
        self.assertIn(("S02", "响应正文凭据字段泄漏"), removed)
        self.assertTrue(any(sid == "S04" for sid, _ in removed))
        # 修复后不应残留任何未消除或新增条目
        self.assertEqual(result["summary"]["present"], 0)
        self.assertEqual(result["summary"]["new"], 0)

        fixed_report = read_report(fixed_report_path)
        for sample in fixed_report["samples"]:
            if sample["sample_id"] in {"S01", "S02", "S04"}:
                self.assertEqual(sample["conclusion"], "合规")

    # 验收 5：样本缺失或不可解析 → 未完成，且无结论
    def test_05_corrupt_and_missing_samples_incomplete(self):
        """验收5：S05 损坏、S06 缺失均为未完成，findings 为空、error 有值。"""

        for sample_id, reason in (("S05", "不可解析"), ("S06", "缺失")):
            sample = self._sample(sample_id)
            self.assertEqual(sample["status"], "未完成", f"{sample_id} 应未完成（{reason}）")
            self.assertEqual(sample["conclusion"], "未完成")
            self.assertEqual(sample["findings"], [])
            self.assertIsNotNone(sample["error"])
        self.assertEqual(self.report["summary"]["incomplete_samples"], 2)

    # 验收 6：部分样本不可读时，其余样本结论保持完整
    def test_06_other_samples_still_evaluated_with_bad_one(self):
        """验收6：两个正常样本 S01/S02 仍给出完整结论，不受 S05/S06 影响。"""

        s01 = self._sample("S01")
        s02 = self._sample("S02")
        self.assertEqual(s01["status"], "完成")
        self.assertEqual(s02["status"], "完成")
        self.assertEqual(s01["conclusion"], CONCLUSION_CONFIRMED)
        self.assertEqual(s02["conclusion"], CONCLUSION_CONFIRMED)
        self.assertGreaterEqual(len(s01["findings"]), 2)
        self.assertGreaterEqual(len(s02["findings"]), 2)
        # 合规样本同样保留有效结论
        self.assertEqual(self._sample("S03")["conclusion"], "合规")

    # 验收 7：严重级别只有高/中/低，报告按高→中→低排序
    def test_07_severity_values_and_ordering(self):
        """验收7：全报告级别取值仅限高中低，每个样本内严格按高→中→低排列。"""

        allowed = {SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW}
        for sample in self.report["samples"]:
            levels = [f["severity"] for f in sample["findings"]]
            self.assertTrue(set(levels) <= allowed)
            self.assertEqual(levels, sorted(levels, key=lambda x: SEVERITY_ORDER[x]))

        # S02 同时含高危 password 与中危 token，顺序必须是高在前
        s02_levels = [f["severity"] for f in self._sample("S02")["findings"]]
        self.assertEqual(s02_levels[0], SEVERITY_HIGH)
        self.assertIn(SEVERITY_MEDIUM, s02_levels)

    # 验收 8：同一暴露重复出现合并为一条并注明次数
    def test_08_duplicate_exposure_merged_with_count(self):
        """验收8：S02 中 token 字段出现两次，合并为一条，occurrences 为 2。"""

        s02 = self._sample("S02")
        token_findings = [f for f in s02["findings"] if f["field_name"] == "token"]
        self.assertEqual(len(token_findings), 1)
        self.assertEqual(token_findings[0]["occurrences"], 2)
        # 位置定位同时保留两处字段路径
        self.assertIn("nested.token", token_findings[0]["location"])
        self.assertIn("backup.token", token_findings[0]["location"])

    # 验收 9：已确认暴露与可疑情况不混类
    def test_09_confirmed_vs_suspected_separated(self):
        """验收9：S04 仅有模糊线索，结论为可疑；S01/S02 为已确认暴露。"""

        s04 = self._sample("S04")
        self.assertEqual(s04["conclusion"], CONCLUSION_SUSPECTED)
        self.assertTrue(s04["findings"])
        for finding in s04["findings"]:
            self.assertEqual(finding["conclusion"], CONCLUSION_SUSPECTED)
            self.assertEqual(finding["severity"], SEVERITY_LOW)

        for sample_id in ("S01", "S02"):
            sample = self._sample(sample_id)
            self.assertEqual(sample["conclusion"], CONCLUSION_CONFIRMED)
            self.assertTrue(
                all(f["conclusion"] == CONCLUSION_CONFIRMED for f in sample["findings"])
            )

    # 验收 10：处置说明只描述移除/替换内容，不含代码片段
    def test_10_remediation_has_no_code_snippet(self):
        """验收10：所有 remediation 为自然语言处置说明，不含引号包裹的代码或反引号。"""

        for sample in self.report["samples"]:
            for finding in sample["findings"]:
                text = finding["remediation"]
                self.assertIsNotNone(text)
                self.assertNotIn("`", text)
                # 不允许出现大段代码形态：花括号块、分号串联语句、赋值符号序列
                self.assertNotIn("{", text)
                self.assertNotIn("}", text)
                self.assertNotIn("; ", text)
                self.assertNotIn(" = ", text)
                # 处置动词必须指向移除/替换类动作
                self.assertTrue(("移除" in text) or ("替换" in text))

    # 验收 11：确定性输出，连续两次运行逐字节相同
    def test_11_byte_for_byte_deterministic(self):
        """验收11：同一输入两次扫描的输出文件逐字节一致。"""

        second = self.workspace / "report_second_run.json"
        write_report(scan(self.scan_root), second)
        self.assertEqual(
            self.report_path.read_bytes(),
            second.read_bytes(),
            "两次运行的报告必须逐字节相同",
        )

    # 验收 12：统一字段名，缺失值用 null 而非空字符串
    def test_12_null_instead_of_empty_string(self):
        """验收12：无建议/不适用字段为 null；全报告不允许空字符串占位。"""

        # S04 文本类线索没有字段名 → field_name 必须是 null
        for finding in self._sample("S04")["findings"]:
            self.assertIsNone(finding["field_name"])
        # 正常样本没有错误 → error 必须是 null
        for sample_id in ("S01", "S02", "S03", "S04"):
            self.assertIsNone(self._sample(sample_id)["error"])
        # 未完成样本没有条目，但字段结构仍统一
        for sample_id in ("S05", "S06"):
            self.assertEqual(self._sample(sample_id)["findings"], [])

        raw = json.loads(self.report_path.read_text(encoding="utf-8"))

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotEqual(value, "", f"字段 {key} 不允许空字符串，应使用 null")
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(raw)


class DetorRuleUnitTest(unittest.TestCase):
    """检测器级别补充用例：私钥块（确认高危）与无版本 Server 头（低危确认）。"""

    def test_private_key_block_is_confirmed_high(self):
        body = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
        findings = detect({"Content-Type": "text/plain"}, body, None)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, SEVERITY_HIGH)
        self.assertEqual(findings[0].conclusion, CONCLUSION_CONFIRMED)
        self.assertIsNone(findings[0].field_name)
        self.assertIn("第1行", findings[0].location)

    def test_server_without_version_is_low_risk(self):
        findings = detect({"Server": "nginx"}, "{}", {})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, SEVERITY_LOW)
        self.assertEqual(findings[0].field_name, "Server")

    def test_no_finding_means_clean(self):
        self.assertEqual(detect({"Content-Type": "application/json"}, '{"status":"ok"}', {"status": "ok"}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
