"""Deterministically generate the sample subtitle manifest and frame PNGs.

The sample covers every rule exercised by the checker: normal entries, safe-area
overflow on all four sides, FX occlusion (including an exact 25% case), multiline
stacking, inverted/zero durations, overlapping intervals, a missing frame and an
undecodable frame.

Run twice and the output is byte-identical:

    python tools/make_sample.py --out samples/case_main

Text geometry follows the checker's model (see README.md):
full-width (CJK) characters advance 1.00 x font size, other characters 0.60 x
font size, line height = round(font_size * 1.20), lines stacked vertically, and
the anchor is the centre of the whole block.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw


CANVAS_W = 1080
CANVAS_H = 1920
SAFE_MARGIN = 120  # top/right/bottom/left

# FX strip along the bottom of the 1080x1920 canvas (above the bottom safe edge).
FX_RECT = {"x": 0, "y": 1640, "width": 1080, "height": 200}


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


# 195 full-width characters (within the 200-character sample limit), wrapped
# into 9 lines of at most 23 characters: 23 * 36 = 828 px <= 840 px safe width.
LONG_TEXT = "\n".join(_chunks("竖屏短视频字幕清单核对" * 15, 23))

# id, start_ms, end_ms, text, font_size, anchor (x, y)
FIXED_SUBTITLES = [
    # Pass: comfortably inside the safe area.
    ("S001", 0, 1800, "开场：城市清晨", 56, (540, 700)),
    # Occlusion: box 360x72 at top 1586..1658, FX top 1640 -> 18 px band,
    # 18/72 = exactly 0.25 of the text box.
    ("S002", 2000, 3600, "字幕遮挡测试", 60, (540, 1622)),
    # Timing anomaly: start after end.
    ("S003", 5200, 4000, "起止倒置条目", 56, (540, 800)),
    # Timing anomaly: zero duration.
    ("S004", 6000, 6000, "零时长条目", 56, (540, 900)),
    # Top overflow: box top = 60 - 86/2 = 17, safe top 120 -> 103 px.
    ("S005", 6400, 8000, "顶部越界测试", 72, (540, 60)),
    # Multiline; the whole block is judged, so the last line crosses the bottom
    # safe edge by 100 px (box 1599..1800..1900 vs safe bottom 1800).
    ("S006", 8400, 10000, "第一行文本\n第二行文本\n第三行文本", 56, (540, 1800)),
    # Multiline, fully inside.
    ("S007", 10400, 12000, "多行正常\n第二行", 48, (540, 1200)),
    # Missing frame on disk -> unverified.
    ("S008", 12400, 14000, "帧图缺失条目", 56, (540, 1000)),
    # Corrupt frame on disk -> unverified.
    ("S009", 14400, 16000, "帧图损坏条目", 56, (540, 1100)),
    # Small occlusion: an 8 px band of the 58 px box (13.79%, below a quarter).
    ("S010", 16400, 18000, "小面积遮挡", 48, (540, 1619)),
    # Left overflow: box left = 30 - 140 = -110, safe left 120 -> 230 px.
    ("S011", 18400, 20000, "左边界越界", 56, (30, 500)),
    # Right overflow: box right = 1030 + 140 = 1170, safe right 960 -> 210 px.
    ("S012", 20400, 22000, "右边界越界", 56, (1030, 600)),
    # Pass, near the top of the canvas.
    ("S013", 22400, 24000, "收尾字幕", 48, (540, 280)),
    # Three intervals that overlap pairwise (three overlap pairs).
    ("S014", 24400, 27000, "重叠一", 48, (540, 1250)),
    ("S015", 25600, 28200, "重叠二", 48, (540, 1320)),
    ("S016", 26800, 29400, "重叠三", 48, (540, 1390)),
    # Nine-line block that fits both horizontally and vertically.
    ("S017", 29800, 31400, LONG_TEXT, 36, (540, 1100)),
    # Pass: box top exactly equals the safe top edge (touching is allowed).
    ("S018", 31800, 33400, "边界贴合", 48, (540, 149)),
    # Both bottom overflow and FX occlusion; overflow wins the report group.
    ("S019", 33800, 35400, "双重异常", 120, (540, 1830)),
]


def text_block_size(text: str, font_size: float) -> tuple[int, int]:
    """Same model as subtitle_checker.geometry.measure_text (for self-checks)."""
    lines = text.split("\n")
    line_height = int(font_size * 1.20 + 0.5)
    widths = [int(len(line) * font_size + 0.5) for line in lines]  # sample is all CJK
    return max(widths), line_height * len(lines)


def make_inputs(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    subtitles = [
        {
            "id": sid,
            "start_ms": start,
            "end_ms": end,
            "text": text,
            "font_size": font_size,
            "anchor": {"x": anchor[0], "y": anchor[1]},
        }
        for sid, start, end, text, font_size, anchor in FIXED_SUBTITLES
    ]

    for sub in subtitles:
        if len(sub["text"]) > 200:
            raise AssertionError(f"sample text for {sub['id']} exceeds 200 chars")

    manifest = {
        "canvas": {"width": CANVAS_W, "height": CANVAS_H},
        "safe_margin": {
            "top": SAFE_MARGIN,
            "right": SAFE_MARGIN,
            "bottom": SAFE_MARGIN,
            "left": SAFE_MARGIN,
        },
        "fx_region": dict(FX_RECT),
        "subtitles": subtitles,
    }

    # Deterministic tiny frames: 240x420 RGB PNGs.
    for sub in subtitles:
        sid = sub["id"]
        if sid == "S008":
            continue  # deliberately missing frame
        path = frames_dir / f"{sid}.png"
        if sid == "S009":
            path.write_bytes(b"\x89PNG\r\n\x1a\nthis is not a decodable png body")
            continue
        seed = int(hashlib.sha256(sid.encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(seed)
        img = Image.new(
            "RGB",
            (240, 420),
            (18 + rng.randrange(24), 22 + rng.randrange(24), 30 + rng.randrange(24)),
        )
        draw = ImageDraw.Draw(img)
        for _ in range(24):
            x0 = rng.randrange(0, 220)
            y0 = rng.randrange(0, 400)
            draw.rectangle(
                [x0, y0, x0 + rng.randrange(8, 40), y0 + rng.randrange(8, 40)],
                fill=(60 + rng.randrange(120), 70 + rng.randrange(120), 90 + rng.randrange(120)),
            )
        draw.rectangle([0, 0, 239, 22], fill=(240, 240, 240))
        img.save(path, format="PNG", compress_level=6)

    (out_dir / "subtitles.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate deterministic sample inputs")
    ap.add_argument("--out", required=True, help="output directory for sample inputs")
    args = ap.parse_args(argv)
    make_inputs(Path(args.out))
    print(f"sample written to {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
