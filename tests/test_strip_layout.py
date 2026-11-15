"""需求 1：条带水平拼接，宽度等于各帧宽度之和，帧序与清单序号一致。"""

from __future__ import annotations

from PIL import Image

from animstrip.builder import build_strip
from animstrip.parser import load_manifest


def _spec(manifest_path, anim_id):
    parsed = {p.animation_id: p for p in load_manifest(manifest_path)}
    return parsed[anim_id].spec


def test_strip_width_equals_sum_of_frame_widths(manifest_path, output_dir, rows_by_id):
    # 需求 1 核对方式：导出条带像素宽度等于各帧宽度之和
    row = rows_by_id["anim_alpha"]
    strip_path = output_dir / row["strip"]
    with Image.open(strip_path) as strip:
        assert strip.mode == "RGBA"
        actual_width = strip.width

    spec = _spec(manifest_path, "anim_alpha")
    expected_width = 0
    for frame in sorted(spec.frames, key=lambda f: f.index):
        with Image.open(spec.resolve_frame_path(frame)) as img:
            expected_width += img.width
    assert actual_width == expected_width == 48 * 3


def test_frame_order_follows_manifest_index(manifest_path):
    # 清单中 anim_mixsize 的帧故意乱序，合成后必须按 index 顺序排列
    spec = _spec(manifest_path, "anim_mixsize")
    strip, ordered = build_strip(spec)
    assert [f.index for f in ordered] == [0, 1, 2]

    offset_x = 0
    for frame in ordered:
        with Image.open(spec.resolve_frame_path(frame)) as img:
            source = img.convert("RGBA")
        segment = strip.crop((offset_x, 0, offset_x + source.width, strip.height))
        # 段内非透明像素必须与对应源帧完全一致（顺序错位则无法匹配）
        assert _opaque_signature(segment) == _opaque_signature(source)
        offset_x += source.width
    assert offset_x == strip.width


def test_mixed_height_vertically_centered(manifest_path):
    # 最高帧 64px，64x32 的帧应垂直居中（上下各留 16px 透明边）
    spec = _spec(manifest_path, "anim_mixsize")
    strip, ordered = build_strip(spec)
    assert strip.height == 64
    first = ordered[0]  # index 0：64x32
    with Image.open(spec.resolve_frame_path(first)) as img:
        source = img.convert("RGBA")
    segment = strip.crop((0, 0, source.width, strip.height))
    top_band = segment.crop((0, 0, source.width, 16))
    bottom_band = segment.crop((0, 48, source.width, 64))
    assert _all_fully_transparent(top_band)
    assert _all_fully_transparent(bottom_band)


def _opaque_signature(img: Image.Image) -> list[tuple[int, int, int, int]]:
    return [px for px in img.getdata() if px[3] != 0]


def _all_fully_transparent(img: Image.Image) -> bool:
    return all(px[3] == 0 for px in img.getdata())
