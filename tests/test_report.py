"""需求 7：核对结果 JSON 字段齐全、按动画 ID 排序、与条带对应。"""

from __future__ import annotations

import json
from pathlib import Path

REQUIRED_FIELDS = {
    "animation_id",
    "target_duration_ms",
    "frame_count",
    "actual_total_ms",
    "diff_ms",
    "passed",
    "strip",
    "issues",
}


def test_report_file_written_as_json(output_dir, report):
    report_path = output_dir / "result.json"
    assert report_path.is_file()
    on_disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert on_disk == report


def test_rows_sorted_by_animation_id(report):
    ids = [row["animation_id"] for row in report["animations"]]
    assert ids == sorted(ids)
    # 正常与异常混跑：全部 9 个条目均在结果中
    assert len(ids) == 9


def test_every_row_has_all_fields(report):
    for row in report["animations"]:
        assert REQUIRED_FIELDS.issubset(row.keys())
        assert isinstance(row["issues"], list)
        assert isinstance(row["passed"], bool)


def test_strip_field_matches_files_on_disk(report, output_dir):
    for row in report["animations"]:
        if row["strip"] is None:
            assert row["passed"] is False
            continue
        strip_path = output_dir / row["strip"]
        assert strip_path.is_file()
        assert strip_path.name == f"{row['animation_id']}.png"
