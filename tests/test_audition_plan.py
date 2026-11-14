"""围绕 9 条业务验收点编写的端到端测试。"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from audition_plan.core import (
    CSV_FIELDS,
    InputError,
    OVERLAP_WARNING,
    plan_audition,
    write_outputs,
)
from audition_plan.sample_data import SCENARIOS, write_scenario

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class AuditionPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scenario(self, name: str) -> Path:
        return write_scenario(self.root, name)

    # (1) 片段数量少于事件：第 3 个事件缺失，前 2 个用不同片段
    def test_insufficient_clips(self) -> None:
        root = self._scenario("01_insufficient_clips")
        result = plan_audition(root / "events.json", root / "clips.json")
        self.assertEqual([row.clip.clip_id for row in result.rows[:2]], ["hit_a", "hit_b"])
        self.assertIsNone(result.rows[2].clip)
        self.assertEqual(result.summary()["matched_count"], 2)
        self.assertEqual(result.summary()["missing_event_count"], 1)

    # (2) 必需标签无匹配：事件缺失，其他事件照常匹配
    def test_tag_no_match(self) -> None:
        root = self._scenario("02_tag_no_match")
        result = plan_audition(root / "events.json", root / "clips.json")
        missing, healed = result.rows
        self.assertIsNone(missing.clip)
        self.assertTrue(missing.missing_reason)
        self.assertEqual(healed.clip.clip_id, "heal_soft")

    # (3) CSV 表头、缺失行片段列为空且原因非空
    def test_csv_shape(self) -> None:
        root = self._scenario("base")
        result = plan_audition(root / "events.json", root / "clips.json")
        csv_path, _ = write_outputs(result, self.root / "out")
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle))
        self.assertEqual(header, CSV_FIELDS)
        rows = _read_csv(csv_path)
        self.assertEqual(len(rows), len(result.rows))
        for row in rows:
            if not row["clip_id"]:
                self.assertTrue(row["missing_reason"])
            else:
                self.assertFalse(row["missing_reason"])
            self.assertRegex(row["start_time_sec"], r"^-?\d+\.\d{3}$")

    # (4) 间隔 0.5 秒、时长 1 秒：两行都标注重叠
    def test_overlap_warning(self) -> None:
        root = self._scenario("04_overlap")
        result = plan_audition(root / "events.json", root / "clips.json")
        csv_path, _ = write_outputs(result, self.root / "out")
        rows = _read_csv(csv_path)
        self.assertEqual([row["overlap_warning"] for row in rows], [OVERLAP_WARNING] * 2)
        self.assertEqual(result.summary()["overlap_warning_count"], 2)

    # 非相邻行互相重叠（A 长片段跨过 B，B 与 C 也重叠）也应标记
    def test_overlap_detects_non_adjacent(self) -> None:
        events = {"events": [
            {"event_id": "a", "time_sec": 0.0, "required_tags": ["x"]},
            {"event_id": "b", "time_sec": 0.1, "required_tags": ["x"]},
            {"event_id": "c", "time_sec": 0.2, "required_tags": ["x"]},
        ]}
        clips = {"clips": [
            {"clip_id": "c1", "path": "c1.wav", "tags": ["x"], "duration_sec": 3.0},
            {"clip_id": "c2", "path": "c2.wav", "tags": ["x"], "duration_sec": 0.1},
            {"clip_id": "c3", "path": "c3.wav", "tags": ["x"], "duration_sec": 0.1},
        ]}
        (self.root / "e.json").write_text(json.dumps(events), encoding="utf-8")
        (self.root / "c.json").write_text(json.dumps(clips), encoding="utf-8")
        for clip_id in ("c1", "c2", "c3"):
            (self.root / f"{clip_id}.wav").write_bytes(b"RIFF")
        result = plan_audition(self.root / "e.json", self.root / "c.json")
        self.assertEqual(result.rows[0].clip.clip_id, "c1")
        self.assertTrue(all(row.overlap for row in result.rows))

    # (5) 文件缺失的片段记为不可用并跳过，其他片段正常
    def test_unusable_clip_file(self) -> None:
        root = self._scenario("05_missing_file")
        result = plan_audition(root / "events.json", root / "clips.json")
        self.assertEqual([entry["clip_id"] for entry in result.unusable_clips], ["ice_gone"])
        self.assertIn("ice_gone.wav", result.unusable_clips[0]["path"])
        assigned = [row.clip.clip_id if row.clip else None for row in result.rows]
        self.assertEqual(assigned, ["boom_ok", "ice_ok"])

    # 目录/空文件之外的不可读场景：用目录冒充音频文件
    def test_unreadable_clip_directory(self) -> None:
        (self.root / "not_a_file.wav").mkdir()
        events = {"events": [{"event_id": "e", "time_sec": 0.0, "required_tags": ["x"]}]}
        clips = {"clips": [
            {"clip_id": "dir_clip", "path": "not_a_file.wav", "tags": ["x"], "duration_sec": 1.0},
            {"clip_id": "good", "path": "good.wav", "tags": ["x"], "duration_sec": 1.0},
        ]}
        (self.root / "e.json").write_text(json.dumps(events), encoding="utf-8")
        (self.root / "c.json").write_text(json.dumps(clips), encoding="utf-8")
        (self.root / "good.wav").write_bytes(b"RIFF")
        result = plan_audition(self.root / "e.json", self.root / "c.json")
        self.assertEqual(result.unusable_clips[0]["clip_id"], "dir_clip")
        self.assertEqual(result.rows[0].clip.clip_id, "good")

    # (6) 时间 2,1,2 → 排序为 1,2,2，重复时间按出现顺序匹配不同片段
    def test_sorting_and_duplicate_time(self) -> None:
        root = self._scenario("06_unordered_duplicate")
        result = plan_audition(root / "events.json", root / "clips.json")
        self.assertEqual(
            [(row.event.event_id, row.start_sec) for row in result.rows],
            [("evt_early", 1.0), ("evt_late_first", 2.0), ("evt_late_second", 2.0)],
        )
        self.assertEqual(
            [row.clip.clip_id for row in result.rows], ["idle_a", "idle_b", "idle_c"]
        )

    # (7) 汇总报告字段齐全且数值与 CSV 行数一致
    def test_report_consistency(self) -> None:
        root = self._scenario("base")
        result = plan_audition(root / "events.json", root / "clips.json")
        csv_path, report_path = write_outputs(result, self.root / "out")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(report),
            {"inputs", "summary", "timeline", "unusable_clips"},
        )
        summary = report["summary"]
        for key in (
            "matched_count", "missing_event_count", "overlap_warning_count", "unusable_clip_count",
        ):
            self.assertIn(key, summary)
        rows = _read_csv(csv_path)
        self.assertEqual(len(report["timeline"]), len(rows))
        self.assertEqual(summary["matched_count"], sum(1 for row in rows if row["clip_id"]))
        self.assertEqual(summary["missing_event_count"], sum(1 for row in rows if not row["clip_id"]))
        self.assertEqual(
            summary["overlap_warning_count"], sum(1 for row in rows if row["overlap_warning"])
        )
        self.assertEqual(
            [entry["clip_id"] for entry in report["unusable_clips"]], ["ghost_boom"]
        )

    # 片段恰好用尽时，缺失原因应说明是"被占用"而不是"无匹配标签"
    def test_exhausted_reason(self) -> None:
        root = self._scenario("01_insufficient_clips")
        result = plan_audition(root / "events.json", root / "clips.json")
        self.assertIn("占用", result.rows[2].missing_reason or "")


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_scenario(self.root, "base")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "audition_plan", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    # (8) 指定新目录，退出码 0，生成 CSV 与 JSON
    def test_out_dir_created(self) -> None:
        out = self.root / "nested" / "out"
        self.assertFalse(out.exists())
        proc = self._run(
            "--events", str(self.root / "base" / "events.json"),
            "--clips", str(self.root / "base" / "clips.json"),
            "--out", str(out),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((out / "audition_plan.csv").is_file())
        self.assertTrue((out / "audition_plan_report.json").is_file())

    # (9) 缺字段 → 非零退出码，标准错误含"缺少必需字段"
    def test_missing_field_error(self) -> None:
        events = self.root / "base" / "broken_events.json"
        events.write_text(
            json.dumps({"events": [{"event_id": "e"}]}), encoding="utf-8"
        )
        proc = self._run(
            "--events", str(events),
            "--clips", str(self.root / "base" / "clips.json"),
            "--out", str(self.root / "out"),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("缺少必需字段", proc.stderr)
        self.assertFalse((self.root / "out" / "audition_plan.csv").exists())

    # 非法 JSON 同样返回非零
    def test_bad_json_error(self) -> None:
        events = self.root / "base" / "bad.json"
        events.write_text("{not json", encoding="utf-8")
        proc = self._run(
            "--events", str(events),
            "--clips", str(self.root / "base" / "clips.json"),
            "--out", str(self.root / "out"),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("不是合法 JSON", proc.stderr)


class DeterminismTest(unittest.TestCase):
    def test_generated_samples_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            write_scenario(a, "base")
            write_scenario(b, "base")
            for relative in ("events.json", "clips.json", "audio/fire_burst.wav"):
                self.assertEqual(
                    (Path(a) / "base" / relative).read_bytes(),
                    (Path(b) / "base" / relative).read_bytes(),
                )

    def test_all_declared_scenarios_generate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in SCENARIOS:
                root = write_scenario(tmp, name)
                self.assertTrue((root / "events.json").is_file())
                self.assertTrue((root / "clips.json").is_file())


if __name__ == "__main__":
    unittest.main()
