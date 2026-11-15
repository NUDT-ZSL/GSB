"""需求 4：帧缺失/无法解码时该动画缺席，其余动画照常产出并列出原因。"""

from __future__ import annotations


def test_missing_frame_animation_has_no_strip(rows_by_id):
    row = rows_by_id["anim_missing"]
    assert row["strip"] is None
    assert row["passed"] is False
    assert any("缺失" in issue for issue in row["issues"])


def test_corrupt_frame_animation_has_no_strip(rows_by_id):
    row = rows_by_id["anim_corrupt"]
    assert row["strip"] is None
    assert row["passed"] is False
    assert any("无法解码" in issue for issue in row["issues"])


def test_other_animations_still_produced(rows_by_id, output_dir):
    # 需求 4 核对方式：仅异常动画缺席，其他动画正常
    produced = {row["animation_id"] for row in rows_by_id.values() if row["strip"]}
    assert produced == {"anim_alpha", "anim_mixsize", "anim_loop", "anim_drift"}
    png_files = {p.name for p in output_dir.glob("*.png")}
    assert png_files == {f"{name}.png" for name in produced}


def test_delete_one_frame_only_its_animation_absent(
    manifest_path, sample_dir, output_dir
):
    # 动态删去一个正常动画的帧，重跑后仅该动画缺席
    target = sample_dir / "frames" / "anim_alpha" / "f0.png"
    target.unlink()
    try:
        report = __import__(
            "animstrip.pipeline", fromlist=["run_pipeline"]
        ).run_pipeline(manifest_path, output_dir)
    finally:
        # 夹具目录为会话级临时目录，恢复样本以免影响后续用例
        from animstrip.samplegen import generate

        generate(sample_dir)

    rows = {row["animation_id"]: row for row in report["animations"]}
    assert rows["anim_alpha"]["strip"] is None
    assert any("缺失" in i for i in rows["anim_alpha"]["issues"])
    assert rows["anim_loop"]["strip"] is not None
    assert rows["anim_drift"]["strip"] is not None
