"""Requirements 3-6 and 8-9: timing, geometry, occlusion, grouping and overlaps."""

from __future__ import annotations

from pathlib import Path

import pytest

from subtitle_checker.check import (
    GROUP_ORDER,
    analyse_subtitle,
    find_overlaps,
    timing_problem,
)
from subtitle_checker.geometry import (
    Box,
    intersection_area,
    measure_text,
    overflow_against,
    safe_area,
    text_box,
)
from subtitle_checker.manifest import Subtitle, validate_manifest
from tests.conftest import base_manifest


def make_sub(sid: str, start: int, end: int, text: str = "示例字幕", font_size: float = 48, x: float = 540, y: float = 800) -> Subtitle:
    return Subtitle(index=0, id=sid, start_ms=start, end_ms=end, text=text, font_size=font_size, anchor_x=x, anchor_y=y)


def empty_frames(tmp_path: Path) -> Path:
    path = tmp_path / "frames"
    path.mkdir(parents=True, exist_ok=True)
    return path


def frames_with(tmp_path: Path, *ids: str) -> Path:
    """Frames directory containing a tiny valid PNG for each id."""
    from PIL import Image

    path = empty_frames(tmp_path)
    for sid in ids:
        Image.new("RGB", (16, 28), (40, 60, 80)).save(path / f"{sid}.png")
    return path


def test_inverted_and_zero_duration_are_timing(sample_root: Path) -> None:
    report = __import__("json").loads((sample_root / "subtitles.json").read_text(encoding="utf-8"))
    manifest = validate_manifest(report, sample_root / "subtitles.json")
    results = [analyse_subtitle(s, manifest, sample_root / "frames") for s in manifest.subtitles]
    anomalies = [r for r in results if r.group == "timing"]
    assert [r.id for r in anomalies] == ["S003", "S004"]
    assert all(r.render_skipped for r in anomalies)
    # Inverted and zero-duration entries must not produce geometry at all.
    assert all(r.box is None and r.metrics is None for r in anomalies)


def test_timing_problem_detection() -> None:
    assert timing_problem(make_sub("A", 1000, 900)) is not None
    assert timing_problem(make_sub("A", 1000, 1000)) is not None
    assert timing_problem(make_sub("A", 1000, 1001)) is None


def test_top_overflow_pixels_are_exact(tmp_path: Path) -> None:
    manifest = validate_manifest(base_manifest(), tmp_path / "m.json")
    sub = make_sub("O1", 0, 1000, text="顶部越界测试", font_size=72, x=540, y=60)
    result = analyse_subtitle(sub, manifest, frames_with(tmp_path, "O1"))
    assert result.group == "overflow"
    assert result.overflow is not None
    assert result.overflow.top == 103
    assert result.overflow.directions() == ["top"]
    assert "顶部越界 103 px" in " ".join(result.reasons)


def test_overflow_none_when_touching_boundary(tmp_path: Path) -> None:
    manifest = validate_manifest(base_manifest(), tmp_path / "m.json")
    metrics = measure_text("边界贴合", 48)
    box = text_box(540, 149, metrics)
    safe = safe_area(1080, 1920, manifest.margin)
    assert box.top == safe.top
    assert not overflow_against(box, safe).triggered


def test_quarter_occlusion_ratio(tmp_path: Path) -> None:
    manifest = validate_manifest(base_manifest(), tmp_path / "m.json")
    sub = make_sub("O2", 0, 1000, text="字幕遮挡测试", font_size=60, x=540, y=1622)
    result = analyse_subtitle(sub, manifest, frames_with(tmp_path, "O2"))
    assert result.group == "occlusion"
    assert result.occlusion_ratio == pytest.approx(0.25, abs=0.01)
    assert result.occlusion_area == 6480


def test_occlusion_ratio_matches_hand_computation() -> None:
    text = Box(left=100, top=100, right=500, bottom=300)
    fx_box = Box(left=0, top=150, right=300, bottom=350)
    area = intersection_area(text, fx_box)
    assert area == 200 * 150
    assert area / text.area == pytest.approx(0.375)


def test_multiline_block_uses_all_lines(tmp_path: Path) -> None:
    manifest = validate_manifest(base_manifest(), tmp_path / "m.json")
    text = "第一行文本\n第二行文本\n第三行文本"
    metrics = measure_text(text, 56)
    assert metrics.line_count == 3
    assert metrics.height == metrics.line_height * 3

    # First line alone would fit; the full block crosses the bottom edge.
    sub = make_sub("M1", 0, 1000, text=text, font_size=56, x=540, y=1800)
    result = analyse_subtitle(sub, manifest, frames_with(tmp_path, "M1"))
    assert result.group == "overflow"
    assert result.overflow is not None
    assert result.overflow.bottom > 0
    assert result.overflow.directions() == ["bottom"]
    assert result.overflow.top == 0


def test_multiline_line_widths_differ() -> None:
    metrics = measure_text("短\n较长的一行", 40)
    assert metrics.width == metrics.line_widths[1] > metrics.line_widths[0]


def test_overlap_pairs_pairwise_sorted(tmp_path: Path) -> None:
    subs = [make_sub("S014", 100, 300), make_sub("S015", 200, 400), make_sub("S016", 250, 500)]
    pairs = find_overlaps(subs)
    assert [(p.first_id, p.second_id) for p in pairs] == [
        ("S014", "S015"),
        ("S014", "S016"),
        ("S015", "S016"),
    ]
    assert all(p.overlap_ms > 0 for p in pairs)


def test_overlap_pairs_skip_timing_anomalies() -> None:
    subs = [make_sub("A", 0, 100), make_sub("B", 50, 150), make_sub("X", 150, 100)]
    assert [p.as_dict() for p in find_overlaps(subs)] == [{"ids": ["A", "B"], "overlap_ms": 50}]


def test_overlap_pairs_ignore_touching_intervals() -> None:
    subs = [make_sub("A", 0, 100), make_sub("B", 100, 200)]
    assert find_overlaps(subs) == []


def test_groups_sorted_by_id_and_all_entries_present(sample_root: Path, tmp_path: Path) -> None:
    from subtitle_checker.report import build_report

    report_data = __import__("json").loads((sample_root / "subtitles.json").read_text(encoding="utf-8"))
    manifest = validate_manifest(report_data, sample_root / "subtitles.json")
    results = [analyse_subtitle(s, manifest, sample_root / "frames") for s in manifest.subtitles]
    report = build_report(manifest, results, find_overlaps(manifest.subtitles), sample_root / "frames")
    grouped = report.grouped()
    assert [name for name in GROUP_ORDER] == ["overflow", "occlusion", "timing", "unverified", "pass"]
    for name, items in grouped.items():
        ids = [item.id for item in items]
        assert ids == sorted(ids), name
    assert grouped["overflow"] and grouped["occlusion"] and grouped["timing"] and grouped["unverified"] and grouped["pass"]
    assert "S001" in [item.id for item in grouped["pass"]]
    assert "S005" in [item.id for item in grouped["overflow"]]
    # Total preserved across groups.
    assert sum(len(items) for items in grouped.values()) == len(manifest.subtitles)


def test_missing_frame_makes_entry_unverified_even_with_overflow(tmp_path: Path) -> None:
    manifest = validate_manifest(base_manifest(), tmp_path / "m.json")
    sub = make_sub("U1", 0, 1000, text="顶部越界测试", font_size=72, x=540, y=60)
    result = analyse_subtitle(sub, manifest, empty_frames(tmp_path))
    assert result.group == "unverified"
    assert any("帧图缺失" in reason for reason in result.reasons)
    # The geometry finding is still recorded.
    assert result.overflow is not None and result.overflow.top == 103
    assert any("顶部越界 103 px" in reason for reason in result.reasons)


def test_overflow_wins_over_occlusion_but_keeps_ratio(tmp_path: Path) -> None:
    manifest = validate_manifest(base_manifest(), tmp_path / "m.json")
    sub = make_sub("B1", 0, 1000, text="双重异常", font_size=120, x=540, y=1830)
    result = analyse_subtitle(sub, manifest, frames_with(tmp_path, "B1"))
    assert result.group == "overflow"
    assert result.occlusion_area > 0
    assert any("遮挡" in reason for reason in result.reasons)
