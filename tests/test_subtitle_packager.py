# -*- coding: utf-8 -*-
"""字幕打包工具验收测试，逐条对应任务核对方式 1-8。

运行：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from subtitle_packager import (  # noqa: E402
    FLAG_BEFORE_HEAD,
    FLAG_CLAMPED,
    FLAG_INVERTED,
    FLAG_OVERLAP,
    FLAG_PAST_TAIL,
    FLAG_ZERO_LENGTH,
    build_package,
)

PARAMS = {
    "target_fps": 25,
    "source_fps": 30,
    "head_seconds": 0.0,
    "tail_seconds": 60.0,
}


def build(entries, params=None):
    return build_package({"entries": entries}, params or PARAMS)


class TestFrameConversion(unittest.TestCase):
    """核对 1：3 条字幕换算帧号与手算一致，文本完整保留。"""

    def test_frames_match_manual_calculation(self):
        entries = [
            # 1.0s*25=25, 3.5s*25=87.5 -> 88（四舍五入）
            {"track": 1, "text": "甲", "start": 1.0, "end": 3.5, "unit": "seconds"},
            # 1500ms=1.5s -> 37.5 -> 38；3200ms=3.2s -> 80
            {"track": 1, "text": "乙", "start": 1500, "end": 3200, "unit": "milliseconds"},
            # 180/30=6s -> 150；210/30=7s -> 175
            {"track": 1, "text": "丙", "start": 180, "end": 210, "unit": "frames"},
        ]
        package, report = build(entries)
        got = [(e["start_frame"], e["end_frame"]) for e in package["entries"]]
        self.assertEqual(got, [(25, 88), (38, 80), (150, 175)])
        self.assertEqual([e["text"] for e in package["entries"]], ["甲", "乙", "丙"])
        self.assertEqual(report["summary"]["invalid"], 0)


class TestSameTrackOverlap(unittest.TestCase):
    """核对 2：同轨重叠两条均标出且轨道号正确，不静默合并。"""

    def test_both_entries_flagged_with_track(self):
        entries = [
            {"track": 2, "text": "A", "start": 10.0, "end": 13.0, "unit": "seconds"},
            {"track": 2, "text": "B", "start": 12.0, "end": 15.0, "unit": "seconds"},
        ]
        package, report = build(entries)
        self.assertEqual(len(package["entries"]), 2)  # 未合并
        self.assertEqual(report["overlaps"], [{"track": 2, "indices": [0, 1]}])
        for e in package["entries"]:
            self.assertIn(FLAG_OVERLAP, e["flags"])

    def test_adjacent_is_not_overlap(self):
        entries = [
            {"track": 2, "text": "A", "start": 10.0, "end": 12.0, "unit": "seconds"},
            {"track": 2, "text": "B", "start": 12.0, "end": 14.0, "unit": "seconds"},
        ]
        _, report = build(entries)
        self.assertEqual(report["overlaps"], [])


class TestCrossTrackOverlapAllowed(unittest.TestCase):
    """核对 3：异轨同时间字幕各自保留，不计重叠。"""

    def test_identical_times_on_different_tracks(self):
        entries = [
            {"track": 3, "text": "中文", "start": 20.0, "end": 24.0, "unit": "seconds"},
            {"track": 4, "text": "English", "start": 20.0, "end": 24.0, "unit": "seconds"},
        ]
        package, report = build(entries)
        self.assertEqual(len(package["entries"]), 2)
        self.assertEqual(report["overlaps"], [])
        for e in package["entries"]:
            self.assertNotIn(FLAG_OVERLAP, e["flags"])


class TestOutOfRange(unittest.TestCase):
    """核对 4：跨过片尾的字幕保留原文案并标注越界方向，不截断。"""

    def test_past_tail_kept_and_flagged(self):
        text = "片尾鸣谢名单（这是一条很长很长不会被截断的文案）" * 3
        entries = [
            {"track": 1, "text": text, "start": 58.0, "end": 65.0, "unit": "seconds"},
        ]
        package, report = build(entries)
        (entry,) = package["entries"]
        self.assertEqual(entry["text"], text)  # 原文完整
        self.assertIn(FLAG_PAST_TAIL, entry["flags"])
        self.assertEqual(report["out_of_range"]["past_tail"], [0])
        self.assertEqual(entry["end_frame"], 65 * 25)  # 不截断到片尾

    def test_before_head_flagged(self):
        entries = [
            {"track": 1, "text": "X", "start": -2.0, "end": 1.0, "unit": "seconds"},
        ]
        package, report = build(
            entries, {**PARAMS, "head_seconds": 1.0}
        )
        (entry,) = package["entries"]
        self.assertIn(FLAG_BEFORE_HEAD, entry["flags"])
        self.assertEqual(report["out_of_range"]["before_head"], [0])


class TestInvalidEntries(unittest.TestCase):
    """核对 5：缺字段条目进入问题列表，其余条目数量不减。"""

    def test_missing_fields_isolated(self):
        entries = [
            {"track": 1, "text": "好", "start": 1.0, "end": 2.0, "unit": "seconds"},
            {"track": 1, "text": "缺起止", "unit": "seconds"},  # 下标 1
            {"track": 2, "text": "也好", "start": 3.0, "end": 4.0, "unit": "seconds"},
        ]
        package, report = build(entries)
        self.assertEqual(len(package["entries"]), 2)  # 合法条目照常进包
        self.assertEqual(report["summary"]["invalid"], 1)
        self.assertEqual(report["invalid_entries"][0]["index"], 1)
        self.assertIn("start", report["invalid_entries"][0]["reasons"][0])

    def test_wrong_types_rejected(self):
        entries = [
            {"track": "1", "text": "X", "start": 1.0, "end": 2.0, "unit": "seconds"},
            {"track": 1, "text": "Y", "start": "1.0", "end": 2.0, "unit": "seconds"},
            {"track": 1, "text": "Z", "start": 1.0, "end": 2.0, "unit": "parsecs"},
        ]
        package, report = build(entries)
        self.assertEqual(package["entries"], [])
        self.assertEqual(report["summary"]["invalid"], 3)


class TestZeroLengthAndInverted(unittest.TestCase):
    """核对 6：零长/倒置可辨认，包内无负帧号。"""

    def test_inverted_flagged_no_negative_frames(self):
        entries = [
            {"track": 1, "text": "倒置", "start": 40.0, "end": 38.0, "unit": "seconds"},
            {"track": 1, "text": "零长", "start": 10.0, "end": 10.0, "unit": "seconds"},
            {"track": 1, "text": "负时间", "start": -5.0, "end": -1.0, "unit": "seconds"},
        ]
        package, report = build(entries)
        self.assertEqual(report["anomalies"]["inverted"], [0])
        self.assertEqual(report["anomalies"]["zero_length"], [1])
        self.assertEqual(report["anomalies"]["clamped_to_zero"], [2])
        for e in package["entries"]:
            self.assertGreaterEqual(e["start_frame"], 0)
            self.assertGreaterEqual(e["end_frame"], 0)
        flags_by_text = {e["text"]: e["flags"] for e in package["entries"]}
        self.assertIn(FLAG_INVERTED, flags_by_text["倒置"])
        self.assertIn(FLAG_ZERO_LENGTH, flags_by_text["零长"])
        self.assertIn(FLAG_CLAMPED, flags_by_text["负时间"])


class TestOrdering(unittest.TestCase):
    """核对 7：轨道升序、同轨起始帧升序，与输入顺序无关。"""

    ENTRIES = [
        {"track": 2, "text": "b2", "start": 5.0, "end": 6.0, "unit": "seconds"},
        {"track": 1, "text": "a2", "start": 10.0, "end": 11.0, "unit": "seconds"},
        {"track": 1, "text": "a1", "start": 2.0, "end": 3.0, "unit": "seconds"},
        {"track": 2, "text": "b1", "start": 1.0, "end": 2.0, "unit": "seconds"},
    ]

    def test_sorted_regardless_of_input_order(self):
        package, _ = build(self.ENTRIES)
        keys = [(e["track"], e["start_frame"]) for e in package["entries"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(
            [e["text"] for e in package["entries"]], ["a1", "a2", "b1", "b2"]
        )
        # 打乱输入重跑：除 source_index（指向原始清单下标）外完全一致
        package2, _ = build(list(reversed(self.ENTRIES)))
        strip = lambda pkg: [  # noqa: E731
            {k: v for k, v in e.items() if k != "source_index"}
            for e in pkg["entries"]
        ]
        self.assertEqual(strip(package), strip(package2))
        # source_index 能正确回指各自输入清单
        for e in package2["entries"]:
            self.assertEqual(
                e["text"], list(reversed(self.ENTRIES))[e["source_index"]]["text"]
            )
        # 字段集合固定，便于导入工具稳定读取
        for e in package["entries"]:
            self.assertEqual(
                set(e.keys()),
                {"track", "text", "start_frame", "end_frame", "flags", "source_index"},
            )


class TestSummary(unittest.TestCase):
    """核对 8：汇总数量与问题下标同输入一致。"""

    def test_summary_counts_and_indices(self):
        entries = [
            {"track": 1, "text": "正常", "start": 1.0, "end": 2.0, "unit": "seconds"},
            {"track": 1, "text": "缺字段"},                      # 下标 1：非法
            {"track": 1, "text": "越界", "start": 58.0, "end": 65.0, "unit": "seconds"},
            {"track": 1, "text": "倒置", "start": 9.0, "end": 8.0, "unit": "seconds"},
        ]
        _, report = build(entries)
        s = report["summary"]
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["ok"], 1)
        self.assertEqual(s["flagged"], 2)
        self.assertEqual(s["invalid"], 1)
        self.assertEqual([p["index"] for p in report["invalid_entries"]], [1])
        self.assertEqual(report["out_of_range"]["past_tail"], [2])
        self.assertEqual(report["anomalies"]["inverted"], [3])


if __name__ == "__main__":
    unittest.main()
