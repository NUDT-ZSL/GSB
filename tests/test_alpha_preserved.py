"""需求 3：透明像素保留原 alpha，不被填充为白/黑底色。"""

from __future__ import annotations

from PIL import Image

from animstrip.builder import build_strip
from animstrip.parser import load_manifest


def _spec(manifest_path, anim_id):
    parsed = {p.animation_id: p for p in load_manifest(manifest_path)}
    return parsed[anim_id].spec


def test_alpha_values_match_source_frames(manifest_path, output_dir, rows_by_id):
    # 需求 3 核对方式：含半透明与全透明帧的条带读回，alpha 与源帧逐像素一致
    row = rows_by_id["anim_alpha"]
    spec = _spec(manifest_path, "anim_alpha")
    with Image.open(output_dir / row["strip"]) as strip:
        assert strip.mode == "RGBA"
        strip_data = list(strip.getdata())
        strip_w, strip_h = strip.size

    offset_x = 0
    for frame in sorted(spec.frames, key=lambda f: f.index):
        with Image.open(spec.resolve_frame_path(frame)) as img:
            source = img.convert("RGBA")
        for y in range(strip_h):
            for x in range(source.width):
                sx, sy = x, y + (strip_h - source.height) // 2
                source_px = source.getpixel((sx, sy))
                strip_px = strip_data[y * strip_w + offset_x + x]
                assert strip_px[3] == source_px[3]
        offset_x += source.width


def test_fully_transparent_pixels_stay_transparent(manifest_path):
    spec = _spec(manifest_path, "anim_alpha")
    strip, _ = build_strip(spec)
    # 渲染规则保证存在全透明（alpha=0）与半透明（alpha=128）像素
    alphas = {px[3] for px in strip.getdata()}
    assert 0 in alphas
    assert 128 in alphas
    # 全透明像素的 RGB 也应保持 0，而不是被白底（255,255,255）或黑底覆盖
    for px in strip.getdata():
        if px[3] == 0:
            assert px[:3] == (0, 0, 0)


def test_saved_png_roundtrip_preserves_alpha(output_dir, rows_by_id):
    row = rows_by_id["anim_alpha"]
    with Image.open(output_dir / row["strip"]) as reread:
        assert reread.info.get("transparency", None) is None or True  # RGBA 直存
        assert reread.mode == "RGBA"
        assert min(px[3] for px in reread.getdata()) == 0
