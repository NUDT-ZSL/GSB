"""需求 6：目标帧时长非正数或序号重复的条目被识别，不进入合成。"""

from __future__ import annotations


def test_non_positive_target_duration_rejected(rows_by_id, output_dir):
    for anim_id in ("anim_bad_dur", "anim_bad_dur_neg"):
        row = rows_by_id[anim_id]
        assert row["passed"] is False
        assert row["strip"] is None
        assert row["frame_count"] == 0
        assert any("target_duration_ms" in issue for issue in row["issues"])
    assert not (output_dir / "anim_bad_dur.png").exists()
    assert not (output_dir / "anim_bad_dur_neg.png").exists()


def test_duplicate_index_rejected(rows_by_id, output_dir):
    row = rows_by_id["anim_dup_index"]
    assert row["passed"] is False
    assert row["strip"] is None
    assert any("序号重复" in issue for issue in row["issues"])
    assert not (output_dir / "anim_dup_index.png").exists()


def test_rejected_entries_keep_id_and_target_in_report(rows_by_id):
    row = rows_by_id["anim_bad_dur"]
    assert row["animation_id"] == "anim_bad_dur"
    assert row["target_duration_ms"] == 0
    assert row["actual_total_ms"] is None
    assert row["diff_ms"] is None
