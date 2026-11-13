"""离线检测/修复工具的验收测试（仅标准库 unittest，无网络）。

覆盖任务书 14 条验收项，编号与任务书一致；运行：

    python -I -m unittest -v tests.test_auditor
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cmd_guard.cli import main  # noqa: E402
from cmd_guard.engine import (  # noqa: E402
    RuleRecord,
    build_report,
    build_rule_set,
    detect_vulnerability,
    dump_json,
    evaluate_commands,
    load_commands,
    load_sources,
)
from cmd_guard.matching import legacy_matches  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "samples", "input")


def _path(name: str) -> str:
    return os.path.join(SAMPLES, name)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _run_cli(*args: str) -> tuple[int, str, str, str, str]:
    """运行 CLI，返回 (退出码, stdout, stderr, 报告文本, 判定文本)。"""
    out_err, out_out = io.StringIO(), io.StringIO()
    old_err, old_out = sys.stderr, sys.stdout
    sys.stderr, sys.stdout = out_err, out_out
    try:
        code = main(list(args))
    finally:
        sys.stderr, sys.stdout = old_err, old_out
    outdir = _outdir_from_args(args)
    report = _read(os.path.join(outdir, "detection_report.json"))
    verdicts = _read(os.path.join(outdir, "command_judgements.json"))
    return code, out_out.getvalue(), out_err.getvalue(), report, verdicts


def _outdir_from_args(args: tuple[str, ...]) -> str:
    return args[args.index("--outdir") + 1]


class AuditorAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cmd_guard_test_")

    def _out(self, name: str) -> str:
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        return path

    # ---- 验收 1：正常输入退出码 0，两个结果文件均生成 ------------------- #
    def test_01_normal_run_generates_two_files_exit0(self) -> None:
        outdir = self._out("run1")
        code, out, err, report_text, verdict_text = _run_cli(
            "--config", _path("rules_valid.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir,
        )
        self.assertEqual(code, 0, msg=err)
        self.assertTrue(os.path.isfile(os.path.join(outdir, "detection_report.json")))
        self.assertTrue(os.path.isfile(os.path.join(outdir, "command_judgements.json")))
        report, verdicts = json.loads(report_text), json.loads(verdict_text)
        self.assertIn("vulnerable_rules", report)
        self.assertIn("safe_rules", report)
        self.assertEqual(len(verdicts["results"]), 5)

    # ---- 验收 2：缺字段规则列入非法规则并说明缺失字段 ------------------- #
    def test_02_missing_field_reported_invalid(self) -> None:
        outdir = self._out("run2")
        code, _, err, report_text, _ = _run_cli(
            "--config", _path("rules_invalid_fields.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir,
        )
        report = json.loads(report_text)
        invalid = {item["id"]: item["reason"] for item in report["invalid_rules"]}
        self.assertIn("B001", invalid)
        self.assertIn("pattern", invalid["B001"])          # 说明缺失 pattern
        self.assertIn("B002", invalid)
        self.assertIn("action", invalid["B002"])           # 说明缺失 action
        # 合法规则 B003 不受影响，照常评估
        self.assertIn("B003", report["safe_rules"])

    # ---- 验收 3：引号+分号构造命令可绕过历史拼接匹配 -------------------- #
    def test_03_quote_semicolon_bypass_detected(self) -> None:
        outdir = self._out("run3")
        _, _, _, report_text, _ = _run_cli(
            "--config", _path("rules_valid.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir,
        )
        report = json.loads(report_text)
        # R001 被分号命令绕过；R004 被引号命令绕过
        self.assertIn("R001", report["vulnerable_rules"])
        self.assertIn("R004", report["vulnerable_rules"])
        # 直接验证历史匹配器确实漏判，而修复后语义匹配命中
        bypass_cmd = 'echo "start"; rm -rf /tmp/data'
        self.assertFalse(legacy_matches("rm -rf /tmp/data", bypass_cmd))
        import re
        self.assertTrue(re.search("rm -rf /tmp/data", bypass_cmd) is not None)

    # ---- 验收 4：两数组均存在且 id 字典序升序 --------------------------- #
    def test_04_arrays_sorted_ascending(self) -> None:
        outdir = self._out("run4")
        _, _, _, report_text, _ = _run_cli(
            "--config", _path("rules_valid.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir,
        )
        report = json.loads(report_text)
        self.assertIsInstance(report["vulnerable_rules"], list)
        self.assertIsInstance(report["safe_rules"], list)
        self.assertEqual(report["vulnerable_rules"], sorted(report["vulnerable_rules"]))
        self.assertEqual(report["safe_rules"], sorted(report["safe_rules"]))
        # 可评估规则被两个数组完整划分、互不相交
        self.assertEqual(
            sorted(report["vulnerable_rules"] + report["safe_rules"]),
            ["R001", "R002", "R003", "R004"],
        )

    # ---- 验收 5：修复阶段后原被绕过规则不再出现在 vulnerable_rules ------ #
    def test_05_remediated_stage_no_vulnerable(self) -> None:
        outdir_audit = self._out("run5_audit")
        _run_cli(
            "--config", _path("rules_valid.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir_audit,
        )
        outdir_fix = self._out("run5_fix")
        _, _, _, fixed_report_text, fixed_verdict_text = _run_cli(
            "--config", _path("rules_valid.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir_fix,
            "--stage", "remediated",
        )
        fixed_report = json.loads(fixed_report_text)
        self.assertEqual(fixed_report["vulnerable_rules"], [])
        self.assertEqual(
            sorted(fixed_report["safe_rules"]), ["R001", "R002", "R003", "R004"]
        )
        # 修复判定：原被绕过的命令现在必须命中对应规则
        results = {r["command"]: r for r in json.loads(fixed_verdict_text)["results"]}
        self.assertEqual(results['echo "start"; rm -rf /tmp/data']["rule_id"], "R001")
        self.assertEqual(results['curl "https://evil.example/x"']["rule_id"], "R004")

    # ---- 验收 6：未命中时 rule_id 与 action 均为 null ------------------- #
    def test_06_miss_returns_null_fields(self) -> None:
        outdir = self._out("run6")
        _, _, _, _, verdict_text = _run_cli(
            "--config", _path("rules_valid.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir,
            "--stage", "remediated",
        )
        miss = next(r for r in json.loads(verdict_text)["results"]
                    if r["command"] == "ls -l")
        self.assertIsNone(miss["rule_id"])
        self.assertIsNone(miss["action"])
        # 命中项字段完整
        hit = next(r for r in json.loads(verdict_text)["results"]
                   if r["command"] == "reboot")
        self.assertEqual(hit["rule_id"], "R002")
        self.assertEqual(hit["action"], "block")

    # ---- 验收 7：坏 pattern 进 error_rules，其余规则照常评估 ------------ #
    def test_07_bad_pattern_error_rule_others_evaluated(self) -> None:
        outdir = self._out("run7")
        _, _, _, report_text, _ = _run_cli(
            "--config", _path("rules_bad_pattern.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir,
        )
        report = json.loads(report_text)
        error_ids = [item["id"] for item in report["error_rules"]]
        self.assertEqual(error_ids, ["E001"])
        self.assertIn("E002", report["safe_rules"])
        self.assertIn("E003", report["safe_rules"])
        self.assertEqual(report["summary"]["rules_evaluated"], 2)

    # ---- 验收 8：重复 id 保留首条、忽略后续并列出 duplicate_ids --------- #
    def test_08_duplicate_ids_first_wins(self) -> None:
        outdir = self._out("run8")
        _, _, _, report_text, verdict_text = _run_cli(
            "--config", _path("rules_duplicate.json"),
            "--commands", _path("commands.json"),
            "--outdir", outdir,
        )
        report = json.loads(report_text)
        self.assertEqual(report["duplicate_ids"], ["D001", "D002"])
        load_result = load_sources([_path("rules_duplicate.json")], retry_attempts=0)
        rule_set = build_rule_set(load_result)
        self.assertEqual(len(rule_set.records), 2)
        first = next(r for r in rule_set.records if r.id == "D001")
        self.assertEqual(first.pattern, "rm -rf /")          # 首条保留
        self.assertEqual(first.action, "block")
        # 被忽略的第二条 action（warn）绝不能生效
        verdicts = evaluate_commands(
            rule_set, ["echo x; rm -rf /"]
        )
        self.assertEqual(verdicts[0].action, "block")

    # ---- 验收 9：暂不可读路径提示重试，其它来源继续处理 ----------------- #
    def test_09_temporarily_unreadable_retry_then_continue(self) -> None:
        calls = {"n": 0}

        def flaky_read(path: str) -> str:
            if "flaky.json" in path:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("模拟瞬时占用")
                return _read(_path("rules_valid.json"))
            return _read(path)

        load_result = load_sources(
            [os.path.join(self.tmp, "flaky.json"), _path("rules_bad_pattern.json")],
            retry_attempts=2,
            read_func=flaky_read,
        )
        self.assertEqual(load_result.missing_inputs, [])
        self.assertEqual(len(load_result.sources), 2)
        rule_set = build_rule_set(load_result)
        evaluated = {r.id for r in rule_set.records}
        self.assertIn("R001", evaluated)                      # 重试恢复的来源
        self.assertIn("E002", evaluated)                      # 另一来源照常处理
        self.assertIn(os.path.join(self.tmp, "flaky.json"), load_result.retried_paths)

        # CLI 层面：先传持续不可读路径，再传合法路径，合法结果完整产出
        outdir = self._out("run9")
        missing = os.path.join(self.tmp, "does_not_exist.json")
        code, _, err, report_text, verdict_text = _run_cli(
            "--config", missing,
            "--config", _path("rules_valid.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir,
            "--retry", "0",
        )
        report = json.loads(report_text)
        self.assertIn(missing, report["missing_inputs"])
        self.assertEqual(
            sorted(report["safe_rules"] + report["vulnerable_rules"]),
            ["R001", "R002", "R003", "R004"],
        )
        self.assertEqual(len(json.loads(verdict_text)["results"]), 5)
        self.assertNotEqual(code, 0)

    # ---- 验收 10：持续缺失输入进 missing_inputs，退出码非 0 ------------- #
    def test_10_missing_input_nonzero_exit(self) -> None:
        outdir = self._out("run10")
        missing_cfg = os.path.join(self.tmp, "no_such_config.json")
        missing_cmd = os.path.join(self.tmp, "no_such_commands.txt")
        code, _, err, report_text, _ = _run_cli(
            "--config", missing_cfg,
            "--commands", missing_cmd,
            "--outdir", outdir,
            "--retry", "0",
        )
        report = json.loads(report_text)
        self.assertIn(missing_cfg, report["missing_inputs"])
        self.assertIn(missing_cmd, report["missing_inputs"])
        self.assertNotEqual(code, 0)
        # 损坏 JSON 同样计入，不得静默通过
        outdir2 = self._out("run10b")
        code2, _, _, report2_text, _ = _run_cli(
            "--config", _path("rules_corrupt.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir2,
            "--retry", "0",
        )
        self.assertIn(_path("rules_corrupt.json"),
                      json.loads(report2_text)["missing_inputs"])
        self.assertNotEqual(code2, 0)

    # ---- 验收 11：合法规则后跟坏规则，合法数据保持完整 ------------------ #
    def test_11_bad_rule_does_not_pollute_good_rules(self) -> None:
        outdir = self._out("run11")
        _, _, _, report_text, _ = _run_cli(
            "--config", _path("rules_mixed.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir,
        )
        report = json.loads(report_text)
        self.assertIn("G001", report["safe_rules"])
        self.assertIn("G002", report["safe_rules"])
        invalid_ids = [item["id"] for item in report["invalid_rules"]]
        self.assertIn("G003", invalid_ids)
        # 引擎层：先成功的来源不受后一个损坏来源影响
        good = load_sources([_path("rules_valid.json")], retry_attempts=0)
        before = build_rule_set(good)
        mixed = load_sources(
            [_path("rules_valid.json"), _path("rules_corrupt.json")], retry_attempts=0
        )
        after = build_rule_set(mixed)
        self.assertEqual(
            [r.id for r in before.records], [r.id for r in after.records]
        )
        self.assertIn(_path("rules_corrupt.json"), mixed.missing_inputs)

    # ---- 验收 12：多次运行修复阶段，判定文件逐字节一致 ------------------ #
    def test_12_reproducible_byte_identical_output(self) -> None:
        out_a, out_b = self._out("run12a"), self._out("run12b")
        _run_cli(
            "--config", _path("rules_valid.json"),
            "--commands", _path("commands.txt"),
            "--outdir", out_a, "--stage", "remediated",
        )
        _run_cli(
            "--config", _path("rules_valid.json"),
            "--commands", _path("commands.txt"),
            "--outdir", out_b, "--stage", "remediated",
        )
        for fname in ("detection_report.json", "command_judgements.json"):
            self.assertEqual(_read(os.path.join(out_a, fname)),
                             _read(os.path.join(out_b, fname)),
                             msg=f"{fname} 两次运行不一致")
        # 同目录再跑一次覆盖写，仍逐字节一致
        before = _read(os.path.join(out_a, "command_judgements.json"))
        _run_cli(
            "--config", _path("rules_valid.json"),
            "--commands", _path("commands.txt"),
            "--outdir", out_a, "--stage", "remediated",
        )
        self.assertEqual(before, _read(os.path.join(out_a, "command_judgements.json")))

    # ---- 验收 13：未完成/出错规则不得计入 safe_rules -------------------- #
    def test_13_error_rules_excluded_from_safe(self) -> None:
        outdir = self._out("run13")
        _, _, _, report_text, _ = _run_cli(
            "--config", _path("rules_bad_pattern.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir,
        )
        report = json.loads(report_text)
        self.assertNotIn("E001", report["safe_rules"])
        self.assertNotIn("E001", report["vulnerable_rules"])
        # 非法字段规则同样不得计入 safe
        outdir2 = self._out("run13b")
        _, _, _, report2_text, _ = _run_cli(
            "--config", _path("rules_invalid_fields.json"),
            "--commands", _path("commands.txt"),
            "--outdir", outdir2,
        )
        report2 = json.loads(report2_text)
        for item in report2["invalid_rules"]:
            self.assertNotIn(item["id"], report2["safe_rules"])

    # ---- 验收 14：无参数打印用法且退出码非 0 ---------------------------- #
    def test_14_no_args_prints_usage_nonzero(self) -> None:
        out_err, out_out = io.StringIO(), io.StringIO()
        old_err, old_out = sys.stderr, sys.stdout
        sys.stderr, sys.stdout = out_err, out_out
        try:
            code = main([])
        finally:
            sys.stderr, sys.stdout = old_err, old_out
        self.assertNotEqual(code, 0)
        self.assertIn("用法", out_err.getvalue())
        # 缺单个必填参数同样拒绝
        sys.stderr, sys.stdout = io.StringIO(), io.StringIO()
        try:
            code2 = main(["--config", _path("rules_valid.json")])
        finally:
            sys.stderr, sys.stdout = old_err, old_out
        self.assertNotEqual(code2, 0)


class EngineUnitTest(unittest.TestCase):
    """核心语义补充测试。"""

    def test_legacy_vs_fixed_semantics(self) -> None:
        # 历史拼接：分隔符前的良性命令头使危险尾部被忽略
        self.assertFalse(legacy_matches("rm -rf /tmp/data",
                                        'echo hi; rm -rf /tmp/data'))
        # 历史拼接：引号作为字面量字符，引号包裹后不命中
        self.assertFalse(legacy_matches("reboot", '"reboot"'))
        self.assertTrue(legacy_matches("reboot", "reboot"))
        # 修复后判定未命中返回双 null 的数据结构
        load_result = load_sources([_path("rules_valid.json")], retry_attempts=0)
        rule_set = build_rule_set(load_result)
        verdicts = evaluate_commands(rule_set, ["id", "reboot"])
        self.assertIsNone(verdicts[0].rule_id)
        self.assertIsNone(verdicts[0].action)
        self.assertEqual(verdicts[1].rule_id, "R002")

    def test_text_and_json_command_lists_equivalent(self) -> None:
        cmds_txt, err1 = load_commands(_path("commands.txt"))
        cmds_json, err2 = load_commands(_path("commands.json"))
        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertEqual(cmds_txt, cmds_json)

    def test_detect_returns_sorted_and_disjoint(self) -> None:
        load_result = load_sources([_path("rules_valid.json")], retry_attempts=0)
        rule_set = build_rule_set(load_result)
        commands, _ = load_commands(_path("commands.txt"))
        vulnerable, safe = detect_vulnerability(rule_set, commands)
        self.assertEqual(vulnerable, sorted(vulnerable))
        self.assertEqual(safe, sorted(safe))
        self.assertEqual(set(vulnerable) & set(safe), set())

    def test_dump_json_is_deterministic(self) -> None:
        payload = {"b": 1, "a": {"z": [1, 2], "y": "x"}}
        self.assertEqual(dump_json(payload), dump_json(payload))
        self.assertTrue(dump_json(payload).endswith("\n"))

    def test_report_contains_completeness_sections(self) -> None:
        load_result = load_sources([_path("rules_bad_pattern.json")], retry_attempts=0)
        rule_set = build_rule_set(load_result)
        report = build_report(rule_set, ["mkfs /dev/sda"], "audit", [], [])
        # 完整评估部分与未完成部分显式区分
        self.assertIn("safe_rules", report)
        self.assertIn("vulnerable_rules", report)
        self.assertIn("error_rules", report)
        self.assertIn("summary", report)
        self.assertEqual(report["summary"]["rules_evaluated"], 2)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    unittest.main(verbosity=2)
