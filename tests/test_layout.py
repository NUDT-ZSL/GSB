"""需求 2、3 的排版引擎单元测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cover_preview.layout import MODE_FALLBACK, MODE_INLINE, layout_copy  # noqa: E402
from cover_preview.models import Rect, VideoEntry  # noqa: E402

FONT = r"C:\Windows\Fonts\simhei.ttf"


def _entry(
    copy_text: str,
    box: Rect | None,
    min_size: int = 40,
    max_size: int = 96,
    canvas: tuple[int, int] = (1080, 1920),
    bar_height: int = 160,
) -> VideoEntry:
    return VideoEntry(
        video_id="T",
        canvas_width=canvas[0],
        canvas_height=canvas[1],
        candidates=[],
        copy_text=copy_text,
        protection_box=box,
        min_font_size=min_size,
        max_font_size=max_size,
        bar_height=bar_height,
        order=0,
    )


def test_shrink_until_clear_of_protection_box():
    # 保护框上沿压入文案区：大字号必然相撞，必须缩字号后完全避让
    entry = _entry("清凉一夏 冰点优惠", Rect(60, 120, 960, 1200))
    result = layout_copy(entry, FONT)
    assert result.mode == MODE_INLINE
    assert result.font_size < entry.max_font_size
    assert result.font_size >= entry.min_font_size
    assert result.union_box is not None
    assert not result.union_box.intersects(entry.protection_box)


def test_fallback_when_min_size_still_collides():
    # 保护框几乎占满画面区：最小字号仍相交 -> 回退条
    entry = _entry("全场五折 仅此一天", Rect(40, 60, 1000, 1580))
    result = layout_copy(entry, FONT)
    assert result.mode == MODE_FALLBACK
    assert result.font_size == entry.min_font_size
    assert result.union_box is not None
    bar = Rect(0, 1920 - 160, 1080, 160)
    # 文案整体位于下方条内
    assert result.union_box.x >= bar.x
    assert result.union_box.right <= bar.right
    assert result.union_box.y >= bar.y
    assert result.union_box.bottom <= bar.bottom


def test_no_protection_box_keeps_max_size():
    entry = _entry("全景大片", None)
    result = layout_copy(entry, FONT)
    assert result.mode == MODE_INLINE
    assert result.font_size == entry.max_font_size


def test_long_text_wraps_and_fits_canvas():
    entry = _entry(
        "超长文案测试：新人下载注册即领千元大礼包还有机会抽取限定周边好礼",
        Rect(80, 260, 920, 1100),
        min_size=36,
        max_size=90,
    )
    result = layout_copy(entry, FONT)
    assert len(result.lines) > 1
    assert result.union_box is not None
    assert result.union_box.right <= entry.canvas_width - 12
    if result.mode == MODE_INLINE:
        assert not result.union_box.intersects(entry.protection_box)
    else:
        assert result.font_size >= entry.min_font_size


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
