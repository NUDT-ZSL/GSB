#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audio_sync 核对工具的本地测试（标准库 unittest，无第三方依赖）。

推荐运行（隔离模式，防止工作目录中的同名模块被导入）：
    python -I tests/test_sync_check.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_sync_check():
    spec = importlib.util.spec_from_file_location("sync_check", ROOT / "audio_sync" / "sync_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_check"] = module  # Python 3.10 dataclass 按模块名回溯注解，必须先注册
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


sc = load_sync_check()
SAMPLES = ROOT / "samples"
CONFIG = ROOT / "config" / "config.sample.json"


def run_tool(testcase: unittest.TestCase, audio: Path, engine: Path,
             config: Path = CONFIG) -> tuple[Path, dict, dict, str]:
    """执行一次核对，返回 (输出目录, result.json, corrections.json, 报告文本)。"""
    outdir = Path(tempfile.mkdtemp(prefix="audio_sync_out_"))
    code = sc.run(audio, engine, config, outdir)
    testcase.assertEqual(code, 0)
    result = json.loads((outdir / "result.json").read_text(encoding="utf-8"))
    corrections = json.loads((outdir / "corrections.json").read_text(encoding="utf-8"))
    report = (outdir / "sync_report.txt").read_text(encoding="utf-8")
    return outdir, result, corrections, report


def write_tmp(name: str, payload) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="audio_sync_in_"))
    path = tmp / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class MainSampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.outdir, self.result, self.corrections, self.report = run_tool(
            self, SAMPLES / "main" / "audio_cues.json", SAMPLES / "main" / "engine_events.json"
        )

    def test_corrections_order_and_values(self):
        # 规则 4/5/9：Camera_Shake -200（绝对值 200）在前，BGM_Start +150 在后；
        # Footstep +50 在容差 80 内，不校正
        names = [c["名称"] for c in self.corrections]
        self.assertEqual(names, ["Camera_Shake", "BGM_Start"])
        self.assertEqual(self.corrections[0], {"名称": "Camera_Shake", "音频时间": 4000, "引擎时间": 3800, "偏移量": -200})
        self.assertEqual(self.corrections[1], {"名称": "BGM_Start", "音频时间": 1200, "引擎时间": 1350, "偏移量": 150})
        self.assertNotIn("Footstep", names)

    def test_order_anomalies(self):
        # 规则 2：音频 Footstep(2000)<Camera_Shake(4000)、重复的 Door_Open(2800)<Door_Open(3000)；
        # 引擎 Camera_Shake(3800)<Light_Flash(4100)
        anomalies = self.result["order_anomalies"]
        self.assertEqual(len(anomalies), 3)
        audio_hits = {(a["name"], a["time"], a["previous_time"])
                      for a in anomalies if a["source"] == "音频"}
        self.assertEqual(audio_hits, {("Footstep", 2000, 4000), ("Door_Open", 2800, 3000)})
        engine_anom = next(a for a in anomalies if a["source"] == "引擎")
        self.assertEqual((engine_anom["name"], engine_anom["time"], engine_anom["previous_time"]),
                         ("Camera_Shake", 3800, 4100))

    def test_uncheckable(self):
        # 规则 3/8：界内重复 Door_Open、引擎缺 name 记录
        # （越界规则在 BoundaryTests 中覆盖）
        items = self.result["uncheckable"]
        reasons = {(i["source"], i["name"]): i["reason"] for i in items}
        self.assertIn(("音频", "Door_Open"), reasons)
        self.assertIn("重复", reasons[("音频", "Door_Open")])
        missing_name = next(i for i in items if i["source"] == "引擎" and i["name"] is None)
        self.assertIn("name", missing_name["reason"])

    def test_unmatched(self):
        # 规则 6：Door_Open 仅音频存在，Light_Flash 仅引擎存在
        unmatched = {(u["source"], u["name"]): u["time"] for u in self.result["unmatched"]}
        self.assertEqual(unmatched, {("音频", "Door_Open"): 3000, ("引擎", "Light_Flash"): 4100})

    def test_report_sections(self):
        # 规则 10：四个分类标题齐全
        for heading in ("一、校正清单", "二、未匹配标记", "三、不可核对标记", "四、顺序异常标记"):
            self.assertIn(heading, self.report)

    def test_inputs_untouched_helper(self):
        # 主样本音频/引擎均读取成功
        self.assertTrue(self.result["files"]["audio"]["readable"])
        self.assertTrue(self.result["files"]["engine"]["readable"])


class BoundaryTests(unittest.TestCase):
    def test_out_of_bounds_negative_and_above(self):
        _, result, corrections, report = run_tool(
            self, SAMPLES / "boundary" / "audio_cues.json", SAMPLES / "boundary" / "engine_events.json"
        )
        # 规则 5：偏移绝对值“超过”阈值才校正，恰好 80 不进入
        self.assertEqual(corrections, [])
        self.assertIn("一、校正清单（按偏移绝对值降序，同值按名称升序）\n  无", report)
        # 规则 7：负时间与超总时长上界均越界，不影响其他标记
        bad = result["uncheckable"]
        self.assertEqual([(i["name"], i["source"]) for i in bad],
                         [("Side_Minus", "音频"), ("Side_Above", "音频")])
        self.assertTrue(all("越界" in i["reason"] for i in bad))


class UnmatchedTests(unittest.TestCase):
    def test_single_sided_sources(self):
        _, result, corrections, _ = run_tool(
            self, SAMPLES / "unmatched" / "audio_cues.json",
            SAMPLES / "unmatched" / "engine_events.json"
        )
        self.assertEqual(corrections, [])
        by_name = {u["name"]: u["source"] for u in result["unmatched"]}
        self.assertEqual(by_name["Only_Audio_1"], "音频")
        self.assertEqual(by_name["Only_Audio_2"], "音频")
        self.assertEqual(by_name["Only_Engine_1"], "引擎")
        self.assertNotIn("Shared_Mark", by_name)  # 双侧存在且偏移 0，不属未匹配


class SortTieTests(unittest.TestCase):
    def test_tie_sorted_by_name(self):
        # 规则 9：Big_Offset(+200) 在前；两个 -120 按名称升序 Alpha_One -> Zebra_Two
        _, _, corrections, _ = run_tool(
            self, SAMPLES / "sort_ties" / "audio_cues.json",
            SAMPLES / "sort_ties" / "engine_events.json"
        )
        self.assertEqual([c["名称"] for c in corrections], ["Big_Offset", "Alpha_One", "Zebra_Two"])
        self.assertEqual([c["偏移量"] for c in corrections], [200, -120, -120])


class BadRecordTests(unittest.TestCase):
    def test_invalid_records_skipped_but_pipeline_continues(self):
        # 规则 8：bool/float 时间、非对象记录、缺 time 字段全部跳过并说明原因
        audio = write_tmp("audio_cues.json", [
            {"name": "Good", "time": 1000},
            {"name": "BoolTime", "time": True},
            {"name": "FloatTime", "time": 12.5},
            "not-a-dict",
            {"name": "NoTime"},
        ])
        engine = write_tmp("engine_events.json", [{"name": "Good", "time": 1090}])
        _, result, corrections, report = run_tool(self, audio, engine)
        self.assertEqual([c["名称"] for c in corrections], ["Good"])  # 其余坏记录不影响 Good 比对
        reasons = {(i["name"]): i["reason"] for i in result["uncheckable"] if i["name"] is not None}
        self.assertIn("类型错误", reasons["BoolTime"])
        self.assertIn("类型错误", reasons["FloatTime"])
        self.assertIn("缺少时间字段", reasons["NoTime"])
        self.assertTrue(any(i["name"] is None and "不是 JSON 对象" in i["reason"]
                            for i in result["uncheckable"]))
        self.assertIn("Good", report)


class ReadFailureTests(unittest.TestCase):
    def test_both_files_unreadable_normal_exit(self):
        # 规则 11：两份表都无法读取，仍输出报告、校正清单为空、退出状态正常
        outdir, result, corrections, report = run_tool(
            self, Path("missing_audio.json"), Path("missing_engine.json")
        )
        self.assertEqual(corrections, [])
        self.assertFalse(result["files"]["audio"]["readable"])
        self.assertFalse(result["files"]["engine"]["readable"])
        self.assertIn("无法读取", report)
        self.assertIn("读取失败说明", report)
        self.assertTrue((outdir / "corrections.json").exists())
        self.assertTrue((outdir / "sync_report.txt").exists())

    def test_one_file_unreadable_other_side_unmatched(self):
        _, result, corrections, report = run_tool(
            self, SAMPLES / "unmatched" / "audio_cues.json", Path("missing_engine.json")
        )
        self.assertEqual(corrections, [])
        self.assertTrue(result["files"]["audio"]["readable"])
        self.assertFalse(result["files"]["engine"]["readable"])
        # 音频侧存活标记全部进入未匹配，来源标注音频
        self.assertTrue(all(u["source"] == "音频" for u in result["unmatched"]))
        self.assertEqual(len(result["unmatched"]), 3)
        self.assertIn("无", report)


class ConfigTests(unittest.TestCase):
    def test_missing_config_uses_default_tolerance(self):
        # 缺省容差 80：偏移 90 应进入校正清单
        audio = write_tmp("audio_cues.json", [{"name": "Mark", "time": 1000}])
        engine = write_tmp("engine_events.json", [{"name": "Mark", "time": 1090}])
        _, result, corrections, report = run_tool(self, audio, engine, Path("no_such_config.json"))
        self.assertEqual(result["config"]["tolerance_ms"], 80)
        self.assertEqual(len(corrections), 1)
        self.assertIn("缺省", report)

    def test_custom_tolerance_respected(self):
        cfg = write_tmp("config.json", {"tolerance_ms": 200, "total_duration_ms": 60000})
        _, _, corrections, _ = run_tool(
            self, SAMPLES / "main" / "audio_cues.json",
            SAMPLES / "main" / "engine_events.json", cfg
        )
        # 容差 200 时 +150 与 -120 都不校正
        self.assertEqual(corrections, [])


class DeterminismTests(unittest.TestCase):
    def test_repeat_runs_byte_identical_and_inputs_unchanged(self):
        # 规则 12：重复运行逐字节一致，且不修改任何输入文件
        inputs = [
            SAMPLES / "main" / "audio_cues.json",
            SAMPLES / "main" / "engine_events.json",
            CONFIG,
        ]
        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        before = {p: digest(p) for p in inputs}
        out1, _, _, _ = run_tool(self, inputs[0], inputs[1])
        mid = {p: digest(p) for p in inputs}
        out2, _, _, _ = run_tool(self, inputs[0], inputs[1])
        after = {p: digest(p) for p in inputs}

        self.assertEqual(before, mid)
        self.assertEqual(mid, after)
        for name in ("corrections.json", "result.json", "sync_report.txt"):
            self.assertEqual((out1 / name).read_bytes(), (out2 / name).read_bytes(),
                             f"{name} 两次输出不一致")


if __name__ == "__main__":
    # 与 `python -I` 配合：确认运行在隔离模式下
    if not sys.flags.isolated:
        print("提示：建议使用 python -I 运行本测试（当前未启用隔离模式）", file=sys.stderr)
    unittest.main(verbosity=2)
