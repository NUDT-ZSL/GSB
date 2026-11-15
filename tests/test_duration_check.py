"""需求 2：时长配平核对，差值单位为毫秒且与手算一致。"""

from __future__ import annotations


def test_balanced_animation_has_zero_diff(rows_by_id):
    row = rows_by_id["anim_alpha"]
    assert row["actual_total_ms"] == 300
    assert row["target_duration_ms"] == 300
    assert row["diff_ms"] == 0
    assert row["passed"] is True


def test_drift_animation_reports_hand_calculated_diff(rows_by_id):
    # 需求 2 核对方式：120+120+100+110=450，目标 400，差值 +50 毫秒
    row = rows_by_id["anim_drift"]
    assert row["actual_total_ms"] == 450
    assert row["diff_ms"] == 450 - 400 == 50
    assert row["passed"] is False
    assert any("50 毫秒" in issue for issue in row["issues"])


def test_all_duration_fields_are_integer_ms(rows_by_id):
    for row in rows_by_id.values():
        if row["actual_total_ms"] is not None:
            assert isinstance(row["actual_total_ms"], int)
            assert isinstance(row["diff_ms"], int)
