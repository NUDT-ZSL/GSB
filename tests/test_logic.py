"""不需要 ffmpeg 的纯逻辑测试：时间轴、字幕分类、帧规划。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from previewkit.loader import load_inputs
from previewkit.models import Shot, Subtitle, InputFormatError
from previewkit.planner import frame_count, frame_time_ms, plan_frames
from previewkit.subtitles import (
    SubtitleIndex,
    classify_all,
    REASON_OUT_OF_BOUNDS,
    REASON_OUTSIDE_SHOT,
    REASON_REVERSED,
    REASON_UNKNOWN_SHOT,
)
from previewkit.timeline import build_timeline, canonical_order, total_duration_ms


def shots(*specs):
    return [
        Shot(shot_id=sid, start_ms=s, end_ms=e, input_index=i)
        for i, (sid, s, e) in enumerate(specs)
    ]


def subs(*specs):
    return [
        Subtitle(index=i, text=t, start_ms=s, end_ms=e, shot_id=sid)
        for i, (t, s, e, sid) in enumerate(specs)
    ]


# ---------------------------------------------------------------- 需求 1
def test_canonical_order_sorts_disordered_list_and_keeps_ids():
    raw = shots(("S03", 4800, 7200), ("S01", 0, 2400), ("S04", 7200, 9600), ("S02", 2400, 4800))
    ordered = canonical_order(raw)
    assert [s.shot_id for s in ordered] == ["S01", "S02", "S03", "S04"]
    assert [(s.start_ms, s.end_ms) for s in ordered] == [
        (0, 2400),
        (2400, 4800),
        (4800, 7200),
        (7200, 9600),
    ]
    placed, gaps, conflicts = build_timeline(raw)
    assert [p.shot.shot_id for p in placed] == ["S01", "S02", "S03", "S04"]
    assert [p.effective_start_ms for p in placed] == [0, 2400, 4800, 7200]
    assert gaps == [] and conflicts == []
    assert total_duration_ms(placed) == 9600


# ---------------------------------------------------------------- 需求 2
def test_subtitle_inside_shot_is_kept_with_fields_unchanged():
    placed, gaps, _ = build_timeline(shots(("S01", 0, 2000), ("S02", 2000, 4000)))
    composed, unfinished = classify_all(subs(("台词", 300, 1500, "S01")), placed, gaps)
    assert unfinished == []
    assert len(composed) == 1
    sub = composed[0].subtitle
    assert (sub.text, sub.start_ms, sub.end_ms, sub.shot_id) == ("台词", 300, 1500, "S01")


# ---------------------------------------------------------------- 需求 3
def test_subtitle_spanning_cut_is_continuous_on_both_frames():
    placed, gaps, _ = build_timeline(shots(("S01", 0, 2000), ("S02", 2000, 4000)))
    composed, _ = classify_all(subs(("跨切点", 1500, 2600, None)), placed, gaps)
    assert composed[0].shot_ids == ("S01", "S02")
    index = SubtitleIndex(composed)
    # fps=10：2000ms 前后两帧是 1900 与 2000，都读到同一文本
    assert index.texts_at(1900) == ("跨切点",)
    assert index.texts_at(2000) == ("跨切点",)
    frames = plan_frames(placed, gaps, 4000, 10, index, {})
    cut = [f for f in frames if f.shot_id in ("S01", "S02")]
    before = [f for f in cut if f.time_ms == 1900][0]
    after = [f for f in cut if f.time_ms == 2000][0]
    assert before.shot_id == "S01" and after.shot_id == "S02"
    assert before.subtitle_texts == after.subtitle_texts == ("跨切点",)


# ---------------------------------------------------------------- 需求 4
def test_frame_count_and_duration_match_total_length():
    assert frame_count(9600, 10) == 96
    assert frame_count(2000, 12) == 24
    assert frame_time_ms(0, 10) == 0
    assert frame_time_ms(95, 10) == 9500
    assert frame_count(1000, 7) == 7  # 四舍五入，不丢尾帧
    placed, gaps, _ = build_timeline(shots(("S01", 0, 2400), ("S02", 2400, 4800)))
    frames = plan_frames(placed, gaps, 4800, 10, SubtitleIndex([]), {"S01": 0, "S02": 1})
    assert len(frames) == 48
    assert frames[-1].time_ms == 4700
    assert all(f.kind == "shot" for f in frames)


# ---------------------------------------------------------------- 需求 5
def test_unknown_shot_subtitle_is_separated_not_rendered():
    placed, gaps, _ = build_timeline(shots(("S01", 0, 2000)))
    composed, unfinished = classify_all(subs(("幽灵", 500, 900, "S99")), placed, gaps)
    assert composed == []
    assert unfinished[0].reason == REASON_UNKNOWN_SHOT
    assert unfinished[0].subtitle.index == 0
    index = SubtitleIndex(composed)
    assert index.texts_at(700) == ()


# ---------------------------------------------------------------- 需求 6
def test_reversed_and_out_of_bounds_are_listed_but_others_still_compose():
    placed, gaps, _ = build_timeline(shots(("S01", 0, 2000), ("S02", 2000, 4000)))
    composed, unfinished = classify_all(
        subs(
            ("正常字幕", 2500, 3200, "S02"),
            ("倒序", 3000, 2500, None),
            ("越界", 3900, 6000, None),
        ),
        placed,
        gaps,
    )
    assert [c.subtitle.text for c in composed] == ["正常字幕"]
    reasons = [u.reason for u in unfinished]
    assert reasons == [REASON_REVERSED, REASON_OUT_OF_BOUNDS]
    index = SubtitleIndex(composed)
    assert index.texts_at(2500) == ("正常字幕",)
    assert index.texts_at(3100) == ("正常字幕",)
    assert index.texts_at(3300) == ()


# ---------------------------------------------------------------- 需求 7
def test_overlap_conflict_pair_and_cut_uses_earlier_end():
    placed, gaps, conflicts = build_timeline(
        shots(("S_A", 0, 3000), ("S_B", 2200, 5000), ("S_C", 5000, 7000))
    )
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert (conflict.earlier.shot_id, conflict.later.shot_id) == ("S_A", "S_B")
    assert (conflict.overlap_start_ms, conflict.overlap_end_ms) == (2200, 3000)
    segs = [(p.shot.shot_id, p.effective_start_ms, p.effective_end_ms, p.status) for p in placed]
    assert segs == [
        ("S_A", 0, 3000, "ok"),
        ("S_B", 3000, 5000, "head_clamped"),
        ("S_C", 5000, 7000, "ok"),
    ]
    assert total_duration_ms(placed) == 7000


def test_fully_overlapped_shot_is_reported_without_frames():
    placed, gaps, conflicts = build_timeline(shots(("S_A", 0, 3000), ("S_B", 1000, 2000)))
    assert placed[1].status == "fully_overlapped"
    assert total_duration_ms(placed) == 3000
    frames = plan_frames(placed, gaps, 3000, 10, SubtitleIndex([]), {})
    assert all(f.shot_id == "S_A" for f in frames)


# ---------------------------------------------------------------- 需求 8
def test_gap_frames_have_no_shot_and_no_subtitle():
    placed, gaps, _ = build_timeline(shots(("S1", 0, 2000), ("S2", 2000, 4000), ("S3", 5000, 7000)))
    assert [(g.start_ms, g.end_ms) for g in gaps] == [(4000, 5000)]
    # 跨越空档的字幕被拒绝，因此空档帧必然无字幕
    composed, unfinished = classify_all(subs(("骑空档", 3800, 5200, None)), placed, gaps)
    assert composed == []
    assert unfinished[0].reason == REASON_OUTSIDE_SHOT
    index = SubtitleIndex(composed)
    frames = plan_frames(placed, gaps, 7000, 10, index, {})
    gap = [f for f in frames if f.kind == "gap"]
    assert len(gap) == 10
    assert all(f.shot_id is None and f.subtitle_texts == () for f in gap)
    assert [f.time_ms for f in gap] == list(range(4000, 5000, 100))
    # 空档两侧的帧仍是相邻镜头，不会被延续进空档
    assert [f.shot_id for f in frames if f.time_ms in (3900, 5000)] == ["S2", "S3"]


# ---------------------------------------------------------------- 相邻但不重叠
def test_adjacent_shots_create_no_gap():
    placed, gaps, conflicts = build_timeline(shots(("S1", 0, 2000), ("S2", 2000, 4000)))
    assert gaps == [] and conflicts == []
    assert total_duration_ms(placed) == 4000


def test_leading_gap_before_first_shot_is_reported():
    placed, gaps, _ = build_timeline(shots(("S1", 500, 2000)))
    assert [(g.start_ms, g.end_ms) for g in gaps] == [(0, 500)]
    assert total_duration_ms(placed) == 2000


# ---------------------------------------------------------------- 需求 12
def test_one_bad_subtitle_does_not_remove_good_ones_or_good_shots():
    placed, gaps, _ = build_timeline(shots(("S01", 0, 2000), ("S02", 2000, 4000)))
    composed, unfinished = classify_all(
        subs(
            ("好字幕一", 200, 1800, "S01"),
            ("坏字幕", 1500, 2500, "S01"),  # 显式引用但跨出镜头
            ("好字幕二", 2100, 3000, "S02"),
        ),
        placed,
        gaps,
    )
    assert [c.subtitle.text for c in composed] == ["好字幕一", "好字幕二"]
    assert len(unfinished) == 1
    shot_ids = [p.shot.shot_id for p in placed]
    assert shot_ids == ["S01", "S02"]


# ---------------------------------------------------------------- 输入校验
def test_loader_rejects_invalid_json_without_partial_state(tmp_path: Path):
    shots_path = tmp_path / "shots.json"
    subtitles_path = tmp_path / "subs.json"
    shots_path.write_text("{ broken", encoding="utf-8")
    subtitles_path.write_text(json.dumps({"subtitles": []}), encoding="utf-8")
    with pytest.raises(InputFormatError) as exc:
        load_inputs(shots_path, subtitles_path)
    assert "不是合法 JSON" in str(exc.value)


def test_loader_rejects_non_integer_ms(tmp_path: Path):
    shots_path = tmp_path / "shots.json"
    subtitles_path = tmp_path / "subs.json"
    shots_path.write_text(
        json.dumps({"shots": [{"shot_id": "S1", "start_ms": "0", "end_ms": 100}]}),
        encoding="utf-8",
    )
    subtitles_path.write_text(json.dumps({"subtitles": []}), encoding="utf-8")
    with pytest.raises(InputFormatError) as exc:
        load_inputs(shots_path, subtitles_path)
    assert "start_ms 必须是整数" in str(exc.value)


def test_loader_accepts_integer_shot_ids(tmp_path: Path):
    shots_path = tmp_path / "shots.json"
    subtitles_path = tmp_path / "subs.json"
    shots_path.write_text(
        json.dumps({"shots": [{"shot_id": 7, "start_ms": 0, "end_ms": 100}]}),
        encoding="utf-8",
    )
    subtitles_path.write_text(
        json.dumps({"subtitles": [{"text": "x", "start_ms": 10, "end_ms": 90, "shot_id": 7}]}),
        encoding="utf-8",
    )
    loaded_shots, loaded_subs = load_inputs(shots_path, subtitles_path)
    assert loaded_shots[0].shot_id == "7"
    assert loaded_subs[0].shot_id == "7"
