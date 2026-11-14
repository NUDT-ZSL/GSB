"""pytest 测试，逐条对应需求 (1)~(12)。"""
from __future__ import annotations

import io
import json
import os
import sys
import time

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from skill_cards.camera import camera_at, fit_level, nearest_keyframe_gap
from skill_cards.cli import main as cli_main
from skill_cards.model import FIT_CLOSE, FIT_OFF, FIT_TIGHT, Keyframe
from skill_cards.pipeline import run_pipeline
from skill_cards.summary import SUMMARY_FILENAME, build_summary


def base_skill(**overrides):
    entry = {
        "name": "测试技能",
        "duration_ms": 1000,
        "canvas": {"width": 800, "height": 600},
        "camera_keyframes": [
            {"time_ms": 0, "zoom": 1.0, "center": [100.0, 200.0]},
            {"time_ms": 500, "zoom": 2.0, "center": [300.0, 400.0]},
            {"time_ms": 1000, "zoom": 1.5, "center": [200.0, 300.0]},
        ],
        "sounds": [100, 600],
    }
    entry.update(overrides)
    return entry


# ------------------------------------------------ 需求 (1) (7) (8)
def test_valid_skill_completed_and_defective_ones_classified():
    entries = [
        base_skill(name="甲"),
        {"name": "缺字段", "duration_ms": 1000},          # 跳过
        base_skill(name="零时长", duration_ms=0),         # 未完成
        base_skill(name="乙", canvas={"width": 0, "height": 600}),  # 未完成
        "不是对象",                                        # 跳过
    ]
    result = run_pipeline(entries)

    names = {c.page_name for c in result.completed}
    assert names == {"甲"}
    assert {s.page_name for s in result.skipped} == {"缺字段", "#05"}
    assert {u.page_name for u in result.unfinished} == {"零时长", "乙"}
    # 跳过与未完成不重复出现同一技能（需求 8）
    skipped_ids = {s.position for s in result.skipped}
    unfinished_ids = {u.position for u in result.unfinished}
    assert skipped_ids.isdisjoint(unfinished_ids)
    # 原因随条目给出
    assert all(s.reason for s in result.skipped)
    assert all(u.reason for u in result.unfinished)


def test_missing_and_wrong_type_fields_all_go_to_skipped():
    bad_entries = [
        {},
        base_skill(duration_ms="1000"),
        base_skill(canvas={"width": 800}),
        base_skill(canvas={"width": "800", "height": 600}),
        base_skill(camera_keyframes=[]),
        base_skill(camera_keyframes=[{"time_ms": 0, "zoom": 1}]),
        base_skill(sounds=[{"label": "没有t"}]),
        base_skill(sounds=[{"t": "x"}]),
        base_skill(name=123),
    ]
    result = run_pipeline(bad_entries)
    assert result.completed == []
    assert len(result.skipped) == len(bad_entries)
    assert result.unfinished == []


# ------------------------------------------------ 需求 (2)
def test_linear_interpolation_between_frames_and_clamp_ends():
    kfs = [
        Keyframe(0, 1.0, 0.0, 0.0),
        Keyframe(500, 2.0, 100.0, 200.0),
    ]
    zoom, cx, cy = camera_at(kfs, 250)
    assert zoom == pytest.approx(1.5)
    assert cx == pytest.approx(50.0)
    assert cy == pytest.approx(100.0)
    # 首帧之前保持首帧
    assert camera_at(kfs, -50) == (1.0, 0.0, 0.0)
    # 末帧之后保持末帧
    assert camera_at(kfs, 999) == (2.0, 100.0, 200.0)
    # 恰好落在关键帧上
    assert camera_at(kfs, 500) == (2.0, 100.0, 200.0)


# ------------------------------------------------ 需求 (3) (4)
def test_out_of_bounds_marked_and_excluded_from_fit():
    entries = [base_skill(sounds=[250, 1500, -5])]
    result = run_pipeline(entries)
    skill = result.completed[0]
    by_t = {s.t: s for s in skill.sounds}

    assert by_t[1500].out_of_bounds is True
    assert by_t[-5].out_of_bounds is True
    # 越界音效间隔/缩放全部留空
    for t in (1500, -5):
        assert by_t[t].gap_ms is None
        assert by_t[t].zoom is None
        assert by_t[t].fit is None
    # 同技能其他音效正常计算贴合度
    normal = by_t[250]
    assert normal.out_of_bounds is False
    assert normal.gap_ms == pytest.approx(250.0)
    assert normal.zoom == pytest.approx(1.5)
    assert normal.cx == pytest.approx(200.0)
    assert normal.cy == pytest.approx(300.0)
    assert normal.fit == FIT_OFF
    assert skill.out_of_bounds_count == 2


def test_nearest_gap_and_fit_levels():
    kfs = [Keyframe(0, 1.0, 0.0, 0.0), Keyframe(500, 1.0, 0.0, 0.0)]
    gap, nearest = nearest_keyframe_gap(kfs, 460)
    assert gap == pytest.approx(40.0) and nearest == 500.0
    assert fit_level(50) == FIT_TIGHT
    assert fit_level(50.0001) == FIT_CLOSE
    assert fit_level(150) == FIT_CLOSE
    assert fit_level(150.0001) == FIT_OFF


def test_sound_values_match_frame_math():
    entries = [base_skill(sounds=[{"t": 750, "label": "s1"}])]
    skill = run_pipeline(entries).completed[0]
    snd = skill.sounds[0]
    # 750 位于 500~1000 之间：zoom 2.0->1.5，center (300,400)->(200,300)
    assert snd.zoom == pytest.approx(1.75)
    assert (snd.cx, snd.cy) == pytest.approx((250.0, 350.0))
    assert snd.gap_ms == pytest.approx(250.0)
    assert snd.nearest_kf_ms == 500.0


# ------------------------------------------------ 需求 (5)
def test_sorting_cards_and_sounds_stable():
    entries = [
        base_skill(name="丙技能", sounds=[900, 100, 500]),
        base_skill(name="甲技能", sounds=[{"t": 80}, {"t": 20}]),
        base_skill(name="乙技能"),
    ]
    result = run_pipeline(entries)
    assert [c.page_name for c in result.completed] == ["丙技能", "乙技能", "甲技能"]
    for skill in result.completed:
        ts = [s.t for s in skill.sounds]
        assert ts == sorted(ts)
    # 汇总越界数与各页一致
    summary = build_summary(result)
    assert f"越界音效总数：{result.total_out_of_bounds}" in summary


# ------------------------------------------------ 需求 (6)
def test_png_cards_rendered_with_canvas_size_and_oob_marker(tmp_path):
    entries = [
        base_skill(name="出图技能", canvas={"width": 640, "height": 480},
                   sounds=[100, 2000]),
    ]
    result = run_pipeline(entries)
    from skill_cards.render import render_card
    skill = result.completed[0]
    out = tmp_path / skill.filename
    render_card(skill, str(out))
    assert out.exists()
    with Image.open(out) as img:
        assert img.format == "PNG"
        assert img.size == (640, 480)
    # 越界音效以红色绘制：图中存在越界红 (220,38,38) 像素
    with Image.open(out) as img:
        colors = img.convert("RGB").getcolors(maxcolors=1_000_000)
    assert colors is not None
    rgb_set = {rgb for _, rgb in colors}
    assert (220, 38, 38) in rgb_set


# ------------------------------------------------ 需求 (9)
def test_non_array_manifest_rejected(tmp_path, capsys, monkeypatch):
    obj_file = tmp_path / "obj.json"
    obj_file.write_text(json.dumps({"name": "x"}, ensure_ascii=False), encoding="utf-8")
    text_file = tmp_path / "text.json"
    text_file.write_text("普通文本", encoding="utf-8")
    out_dir = tmp_path / "out"

    rc1 = cli_main(["--manifest", str(obj_file), "--out", str(out_dir)])
    rc2 = cli_main(["--manifest", str(text_file), "--out", str(out_dir)])
    assert rc1 != 0 and rc2 != 0
    assert not out_dir.exists()  # 整体拒绝时不创建输出目录、无任何产物
    err = capsys.readouterr().err
    assert "数组" in err and "拒绝" in err


# ------------------------------------------------ 需求 (10)
def test_unicode_space_path_and_missing_manifest(tmp_path, capsys):
    manifest = tmp_path / "清单 目录" / "技能 清单.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps([base_skill()], ensure_ascii=False),
                        encoding="utf-8")
    out_dir = tmp_path / "输出 目录 中文"
    rc = cli_main(["--manifest", str(manifest), "--out", str(out_dir)])
    assert rc == 0
    pngs = list(out_dir.glob("*.png"))
    assert len(pngs) == 1 and (out_dir / SUMMARY_FILENAME).exists()

    missing = tmp_path / "不存在" / "missing.json"
    rc = cli_main(["--manifest", str(missing), "--out", str(out_dir)])
    assert rc != 0
    assert str(missing) in capsys.readouterr().err


# ------------------------------------------------ 需求 (11)
def test_output_dir_created_and_rerun_rewrites_cleanly(tmp_path):
    out_dir = tmp_path / "cards"
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps([base_skill(name="甲"), base_skill(name="乙")],
                                   ensure_ascii=False), encoding="utf-8")
    assert cli_main(["--manifest", str(manifest), "--out", str(out_dir)]) == 0
    first_pngs = sorted(p.name for p in out_dir.glob("*.png"))
    assert len(first_pngs) == 2
    summary_first = (out_dir / SUMMARY_FILENAME).read_text(encoding="utf-8")

    # 第二次清单只保留一个技能：旧卡必须被清掉，汇总无残留条目
    manifest.write_text(json.dumps([base_skill(name="甲")], ensure_ascii=False),
                        encoding="utf-8")
    assert cli_main(["--manifest", str(manifest), "--out", str(out_dir)]) == 0
    second_pngs = sorted(p.name for p in out_dir.glob("*.png"))
    assert len(second_pngs) == 1
    summary_second = (out_dir / SUMMARY_FILENAME).read_text(encoding="utf-8")
    assert "乙" not in summary_second
    # 再跑一次，汇总逐字节一致
    assert cli_main(["--manifest", str(manifest), "--out", str(out_dir)]) == 0
    assert (out_dir / SUMMARY_FILENAME).read_text(encoding="utf-8") == summary_second
    assert len(list(out_dir.glob("*.png"))) == 1


# ------------------------------------------------ 需求 (12)
def test_blank_name_uses_unique_position_index():
    entries = [
        base_skill(name="   "),
        base_skill(name=""),
        base_skill(name="正常名"),
    ]
    result = run_pipeline(entries)
    by_pos = {c.position: c for c in result.completed}
    assert by_pos[1].page_name == "#01"
    assert by_pos[2].page_name == "#02"
    assert by_pos[1].filename.startswith("01_#01")
    assert by_pos[2].filename.startswith("02_#02")
    # 序号在本次输出内唯一
    pages = [c.page_name for c in result.completed]
    assert len(set(pages)) == len(pages)
    summary = build_summary(result)
    assert "#01" in summary and "#02" in summary


def test_bom_manifest_accepted(tmp_path):
    """Windows 编辑器写入的 UTF-8 BOM 清单应能正常解析。"""
    manifest = tmp_path / "bom.json"
    manifest.write_bytes(
        "﻿".encode("utf-8") + json.dumps([base_skill()], ensure_ascii=False).encode("utf-8")
    )
    out_dir = tmp_path / "out"
    assert cli_main(["--manifest", str(manifest), "--out", str(out_dir)]) == 0
    assert len(list(out_dir.glob("*.png"))) == 1


def test_summary_oob_counts_match_each_card():
    entries = [
        base_skill(name="甲", sounds=[100, 5000, -1]),
        base_skill(name="乙", sounds=[200]),
    ]
    result = run_pipeline(entries)
    card_oob = {c.page_name: c.out_of_bounds_count for c in result.completed}
    assert card_oob == {"乙": 0, "甲": 2}
    summary = build_summary(result)
    assert "越界音效总数：2" in summary
