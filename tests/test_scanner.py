"""端到端测试：覆盖任务书 8 个验收点。

运行::

    python -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from vuln_scanner.report import build_report, compare_with_baseline  # noqa: E402
from vuln_scanner.scanner import Scanner  # noqa: E402
from vuln_scanner.security import (  # noqa: E402
    PathSecurityError,
    SecurityLogger,
    check_external_path,
    safe_resolve,
    validate_entry_name,
)

from tools.build_samples import main as build_samples_main  # noqa: E402


def _hash_tree(root: Path) -> dict[str, str]:
    """记录目录内每个文件的 SHA-256，用于证明扫描不会改写原始数据。"""
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digest


class ScannerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build_samples_main()
        cls.raw = PROJECT_ROOT / "samples" / "raw"
        cls.fixed = PROJECT_ROOT / "samples" / "fixed"
        cls.config_good = PROJECT_ROOT / "config" / "rules.json"
        cls.config_broken = PROJECT_ROOT / "config" / "rules_broken.json"
        cls.config_partial = PROJECT_ROOT / "config" / "rules_partial.json"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def run_scan(self, target: Path, config: Path | None = None) -> dict:
        log_path = self.work / f"security_{target.name}.log.jsonl"
        scanner = Scanner(target, SecurityLogger(log_path), config_path=config)
        return build_report(scanner.scan())

    def find_keys(self, report: dict) -> set[tuple[str, str, str]]:
        return {
            (item["file_name"], item["field_path"], item["rule"])
            for item in report["findings"]
        }


class TestAcceptance(ScannerTestBase):
    # ---- 验收点 1：路径遍历名称拒绝，正常文件解析 ----
    def test_01_traversal_name_rejected_and_normal_parsed(self) -> None:
        ok, _ = validate_entry_name("../evil.json")
        self.assertFalse(ok)
        ok, _ = validate_entry_name("sub/dir.json")
        self.assertFalse(ok)
        ok, _ = validate_entry_name("users.json")
        self.assertTrue(ok)

        logger = SecurityLogger(self.work / "sec_01.log.jsonl")
        with self.assertRaises(PathSecurityError):
            safe_resolve(self.raw, "../evil.json", logger)
        with self.assertRaises(PathSecurityError):
            safe_resolve(self.raw, "sub\\dir.json", logger)

        report = self.run_scan(self.raw)
        # 越界名称不允许进入解析结果（该名称在文件系统上根本无法创建）
        self.assertNotIn("../evil.json", report["parsed_files"])
        # 目录内合法 json/csv 全部被解析
        self.assertEqual(
            report["parsed_files"],
            ["departments.json", "orders.csv", "profile.json", "users.json"],
        )

    # ---- 验收点 2：SQL/命令注入特征标记，普通文本不标记 ----
    def test_02_injection_features_flagged_plaintext_not(self) -> None:
        report = self.run_scan(self.raw)
        keys = self.find_keys(report)
        self.assertIn(("users.json", "users[1].remark", "sql_drop_table"), keys)
        drop = next(
            f for f in report["findings"]
            if f["rule"] == "sql_drop_table"
        )
        self.assertIn("DROP TABLE", drop["matched_feature"].upper())
        self.assertEqual(drop["field_path"], "users[1].remark")
        # 普通文本字段不产生任何命中
        flagged_paths = {
            (f["file_name"], f["field_path"]) for f in report["findings"]
        }
        self.assertNotIn(("departments.json", "departments[0].desc"), flagged_paths)
        self.assertNotIn(("orders.csv", "row[1].note"), flagged_paths)
        self.assertNotIn(("users.json", "users[0].remark"), flagged_paths)

    # ---- 验收点 3：报告字段与排序 ----
    def test_03_report_fields_and_sorting(self) -> None:
        report = self.run_scan(self.raw)
        # 内置 8 条规则在基线样本上命中 5 项（SLEEP 等自定义规则不含在内）
        self.assertEqual(report["summary"]["total_findings"], 5)
        required = {"file_name", "vuln_type", "risk_level", "advice",
                    "field_path", "matched_feature"}
        for item in report["findings"]:
            self.assertTrue(required.issubset(item.keys()))
        risk_rank = {"high": 0, "medium": 1, "low": 2}
        ordered = [
            (risk_rank[f["risk_level"]], f["file_name"].lower())
            for f in report["findings"]
        ]
        self.assertEqual(ordered, sorted(ordered))
        # 高危确实排在低危之前
        risks = [f["risk_level"] for f in report["findings"]]
        self.assertLess(risks.index("high"), risks.index("low"))

    # ---- 验收点 4：修复副本对比，原始数据不改写 ----
    def test_04_fixed_comparison_and_immutability(self) -> None:
        before = _hash_tree(self.raw)
        raw_report = self.run_scan(self.raw)
        fixed_report = self.run_scan(self.fixed)
        after = _hash_tree(self.raw)
        self.assertEqual(before, after, "扫描过程改写了原始样本文件")

        comparison = compare_with_baseline(
            Scanner(self.fixed, SecurityLogger(self.work / "cmp.log.jsonl")).scan(),
            raw_report,
        )
        statuses = {
            (item["file_name"], item["field_path"], item["rule"]): item["status"]
            for item in comparison["comparison"]
        }
        # 已修复项
        self.assertEqual(
            statuses[("users.json", "users[1].remark", "sql_drop_table")],
            "fixed",
        )
        # 仍存在项（修复副本刻意保留）
        self.assertEqual(
            statuses[("orders.csv", "row[2].note", "cmd_shell_sep")],
            "open",
        )
        self.assertEqual(comparison["summary"]["fixed"], 4)
        self.assertEqual(comparison["summary"]["still_open"], 1)
        self.assertEqual(comparison["summary"]["new"], 0)
        # 修复副本的报告本身只含 1 条现存命中
        self.assertEqual(fixed_report["summary"]["total_findings"], 1)

    # ---- 验收点 5：损坏文件跳过且标记，其他文件结果完整 ----
    def test_05_corrupt_files_marked_parse_failed(self) -> None:
        report = self.run_scan(self.raw)
        failed = {item["file_name"]: item["status"]
                  for item in report["parse_failures"]}
        self.assertEqual(
            failed,
            {"broken_encoding.csv": "parse_failed",
             "broken_syntax.json": "parse_failed"},
        )
        # 合法文件条目齐全：4 个可解析文件仍产出全部 5 条命中
        self.assertEqual(report["summary"]["parsed_files"], 4)
        self.assertEqual(report["summary"]["total_findings"], 5)

    # ---- 验收点 6：自定义规则合并；坏配置不影响其余检测 ----
    def test_06_custom_rules_and_bad_config(self) -> None:
        # 合法配置：<script> 字面量规则 + SLEEP 正则规则
        with tempfile.TemporaryDirectory() as tmpdir:
            extra = Path(tmpdir) / "xss"
            extra.mkdir()
            (extra / "page.json").write_text(
                json.dumps({"html": "<script>alert(1)</script>"}),
                encoding="utf-8",
            )
            report = self.run_scan(extra, config=self.config_good)
            rule_names = {f["rule"] for f in report["findings"]}
            self.assertIn("custom_xss_script", rule_names)

        report_raw = self.run_scan(self.raw, config=self.config_good)
        rule_names = {f["rule"] for f in report_raw["findings"]}
        self.assertIn("custom_sql_sleep", rule_names)
        # 内置规则依然生效
        self.assertIn("sql_drop_table", rule_names)

        # 语法损坏的配置：报告问题，内置规则照常命中
        broken_report = self.run_scan(self.raw, config=self.config_broken)
        self.assertTrue(broken_report["config_issues"])
        self.assertIn(
            "sql_drop_table",
            {f["rule"] for f in broken_report["findings"]},
        )

        # 部分坏条目：好条目生效，坏条目被报告并跳过
        with tempfile.TemporaryDirectory() as tmpdir:
            partial_dir = Path(tmpdir) / "partial"
            partial_dir.mkdir()
            (partial_dir / "page.json").write_text(
                json.dumps({"html": "<script>x</script>"}),
                encoding="utf-8",
            )
            partial_report = self.run_scan(partial_dir, config=self.config_partial)
        self.assertTrue(partial_report["config_issues"])
        self.assertIn(
            "custom_xss_script",
            {f["rule"] for f in partial_report["findings"]},
        )

    # ---- 验收点 7：越界访问拒绝并写安全日志 ----
    def test_07_out_of_root_denied_and_logged(self) -> None:
        log_path = self.work / "sec_07.log.jsonl"
        logger = SecurityLogger(log_path)
        for probe in ("../outside.json", str(Path.cwd() / "outside.json")):
            with self.assertRaises(PathSecurityError):
                check_external_path(self.raw, probe, logger)
        entries = [
            json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["result"] == "DENIED" for e in entries))
        self.assertTrue(all("path_rejected" == e["event"] for e in entries))

    # ---- 验收点 8：CLI 参数与缺省输出 ----
    def test_08_cli_named_and_default_output(self) -> None:
        base_env = {"PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(PROJECT_ROOT)}
        named_out = self.work / "cli_report.json"
        proc = subprocess.run(
            [sys.executable, "-m", "vuln_scanner",
             "-i", str(self.raw), "-o", str(named_out)],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", env=base_env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(named_out.exists())
        payload = json.loads(named_out.read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["total_findings"], 5)

        # 缺省输出：在临时工作目录生成 report.json
        with tempfile.TemporaryDirectory() as cwd_tmp:
            proc = subprocess.run(
                [sys.executable, "-m", "vuln_scanner",
                 "-i", str(self.raw)],
                cwd=cwd_tmp, capture_output=True, text=True,
                encoding="utf-8", env=base_env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((Path(cwd_tmp) / "report.json").exists())

    def test_09_unsupported_extension_skipped_not_parsed(self) -> None:
        report = self.run_scan(self.raw)
        skipped = {item["file_name"]: item["status"]
                   for item in report["rejected_entries"]}
        self.assertEqual(skipped["notes.txt"], "skipped_unsupported_extension")


if __name__ == "__main__":
    unittest.main(verbosity=2)
