"""确定性生成演示用样例帧、字幕表与面板配置。

用法：python demo/make_demo.py
生成物位于 demo/assets 与 demo/samples；重复运行逐字节一致。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

DEMO_DIR = Path(__file__).resolve().parent
ASSETS = DEMO_DIR / "assets"
SAMPLES = DEMO_DIR / "samples"

CANVAS = (640, 360)
SAFE_AREA = {"left": 32, "top": 18, "right": 608, "bottom": 342}
PANEL = {"left": 528, "top": 60, "right": 600, "bottom": 300}
FRAME_STEP_MS = 100
FRAME_COUNT = 31

STYLE = {
    "font_size": 30,
    "line_spacing": 1.35,
    "padding": 12,
    "stroke_width": 2,
    "fill": "#FFFFFF",
    "stroke_fill": "#000000",
    "onset_review_threshold_ms": 500,
}


def _background(width: int, height: int) -> Image.Image:
    """由坐标唯一决定的渐变底图，不依赖随机数。"""
    small = Image.new("RGB", (32, 18))
    pixels = small.load()
    for y in range(18):
        for x in range(32):
            pixels[x, y] = (
                10 + x * 2,
                16 + y * 3,
                38 + (x * 3 + y * 2) % 32,
            )
    return small.resize((width, height), Image.Resampling.BICUBIC)


def _draw_panel(draw: ImageDraw.ImageDraw) -> None:
    """血条面板：右侧固定区域，内部有条形槽与填充。"""
    box = (PANEL["left"], PANEL["top"], PANEL["right"] - 1, PANEL["bottom"] - 1)
    draw.rectangle(box, fill=(24, 32, 54), outline=(90, 110, 160), width=2)
    for i, ratio in enumerate((0.82, 0.64, 0.47, 0.31)):
        top = PANEL["top"] + 22 + i * 48
        draw.rectangle(
            (PANEL["left"] + 12, top, PANEL["right"] - 13, top + 16),
            fill=(40, 46, 62),
        )
        draw.rectangle(
            (
                PANEL["left"] + 12,
                top,
                PANEL["left"] + 12 + int(ratio * (PANEL["right"] - PANEL["left"] - 26)),
                top + 16,
            ),
            fill=(196, 60, 60) if i % 2 == 0 else (206, 132, 44),
        )
    draw.rectangle(
        (PANEL["left"] + 12, PANEL["bottom"] - 34, PANEL["right"] - 13, PANEL["bottom"] - 18),
        fill=(60, 76, 110),
    )


def make_frame(index: int) -> Image.Image:
    """第 index 帧：每帧内容不同，但只由 index 决定。"""
    image = _background(*CANVAS)
    draw = ImageDraw.Draw(image)
    # 缓慢移动的过场元素，使帧内容逐帧变化。
    x = 40 + (index * 37) % 420
    y = 70 + (index * 53) % 200
    draw.rectangle((x, y, x + 90, y + 60), fill=(70, 96, 150))
    draw.ellipse((x + 18, y + 10, x + 52, y + 44), fill=(220, 200, 120))
    band = (index * 11) % CANVAS[0]
    draw.rectangle((band, 0, band + 12, CANVAS[1]), fill=(30, 44, 76))
    draw.rectangle((0, 300, 260, 316), fill=(18, 24, 40))
    draw.rectangle((0, 300, 40 + (index * 9) % 220, 316), fill=(90, 140, 190))
    _draw_panel(draw)
    return image


def write_frames() -> list[dict[str, object]]:
    frames_dir = ASSETS / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    for index in range(FRAME_COUNT):
        time_ms = index * FRAME_STEP_MS
        name = f"frame_{index:04d}.png"
        make_frame(index).save(frames_dir / name, format="PNG", compress_level=9)
        entries.append(
            {"time_ms": time_ms, "path": f"../../assets/frames/{name}"}
        )
    return entries


def write_broken_frame() -> str:
    """写入一个无法解码的 PNG，用于演示损坏帧跳过。"""
    path = ASSETS / "frames_broken" / "frame_corrupt.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"corrupted payload, not a real image")
    return "../../assets/frames_broken/frame_corrupt.png"


def write_manifest(
    name: str,
    *,
    frames: list[dict[str, object]],
    subtitles: list[dict[str, object]],
    panel=...,
    safe_area: dict[str, int] = SAFE_AREA,
    style: dict[str, object] = STYLE,
) -> Path:
    directory = SAMPLES / name
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
        "safe_area": safe_area,
        "frames": frames,
        "subtitles": subtitles,
        "style": style,
    }
    if panel is not ...:
        payload["panel"] = panel
    path = directory / "manifest.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _frames(*indices: int) -> list[dict[str, object]]:
    return [
        {"time_ms": i * FRAME_STEP_MS, "path": f"../../assets/frames/frame_{i:04d}.png"}
        for i in indices
    ]


def build_samples() -> dict[str, Path]:
    all_frames = [
        {"time_ms": i * FRAME_STEP_MS, "path": f"../../assets/frames/frame_{i:04d}.png"}
        for i in range(FRAME_COUNT)
    ]
    broken_path = write_broken_frame()
    samples: dict[str, Path] = {}

    # 1) 主样例：覆盖正常放置、跨帧、偏移过大、无起音、超行未放置、空白条目。
    samples["main"] = write_manifest(
        "main",
        frames=[
            *all_frames[:10],
            {"time_ms": 1000, "path": "../../assets/frames/frame_0010.png"},
            {"time_ms": 1100, "path": "../../assets/frames/frame_0011.png"},
            {"time_ms": 1200, "path": "../../assets/frames/frame_0012.png"},
            *all_frames[13:],
        ],
        panel=PANEL,
        subtitles=[
            {"start_ms": 300, "end_ms": 700, "text": "风起于青萍之末", "onset_ms": 260},
            {"start_ms": 1050, "end_ms": 1150, "text": "只出现在中间帧", "onset_ms": 1070},
            {"start_ms": 1500, "end_ms": 1900, "text": "起音早于字幕起始的样本", "onset_ms": 900},
            {"start_ms": 2000, "end_ms": 2400, "text": "这段对白没有起音标注"},
            {
                "start_ms": 2500,
                "end_ms": 2900,
                "text": "这是一段很长的旁白文本，用来验证剩余高度不足以容纳第三行时会整段跳过",
                "onset_ms": 2490,
            },
            {"start_ms": 1200, "end_ms": 1400, "text": "   ", "onset_ms": 1200},
        ],
    )

    # 2) 面板区域坐标非法：按无面板处理。
    samples["panel_invalid"] = write_manifest(
        "panel_invalid",
        frames=all_frames[:6],
        panel={"left": 520, "top": 40, "right": 900, "bottom": 300},
        subtitles=[
            {"start_ms": 100, "end_ms": 500, "text": "面板坐标非法时按无面板居中", "onset_ms": 120}
        ],
    )

    # 3) 安全区剩余高度不足：整段未放置。
    samples["tight_height"] = write_manifest(
        "tight_height",
        frames=all_frames[:5],
        safe_area={"left": 32, "top": 150, "right": 608, "bottom": 196},
        panel=None,
        subtitles=[
            {"start_ms": 0, "end_ms": 400, "text": "高度不足时不应出现在预览帧", "onset_ms": 20}
        ],
    )

    # 4) 空字幕表：字幕数组为空，帧照常输出。
    samples["empty_subtitles"] = write_manifest(
        "empty_subtitles",
        frames=all_frames[:5],
        panel=PANEL,
        subtitles=[],
    )

    # 5) 全部文本为空白：字幕数组同样为空。
    samples["blank_subtitles"] = write_manifest(
        "blank_subtitles",
        frames=all_frames[:4],
        panel=PANEL,
        subtitles=[
            {"start_ms": 0, "end_ms": 300, "text": "   ", "onset_ms": 0},
            {"start_ms": 100, "end_ms": 400, "text": "\n\t ", "onset_ms": 100},
        ],
    )

    # 6) 居中参照：无面板，文本框应居中于整个安全区。
    samples["no_panel"] = write_manifest(
        "no_panel",
        frames=all_frames[:3],
        panel=None,
        subtitles=[{"start_ms": 0, "end_ms": 500, "text": "无面板居中", "onset_ms": 10}],
    )

    # 7) 时间窗跨三帧：起止落在中间帧内，只有中间帧应出现文本。
    samples["time_window"] = write_manifest(
        "time_window",
        frames=[
            {"time_ms": 0, "path": "../../assets/frames/frame_0000.png"},
            {"time_ms": 100, "path": "../../assets/frames/frame_0001.png"},
            {"time_ms": 200, "path": "../../assets/frames/frame_0002.png"},
        ],
        panel=PANEL,
        subtitles=[
            {"start_ms": 50, "end_ms": 150, "text": "仅中间帧可见", "onset_ms": 50}
        ],
    )

    # 8) 含损坏帧与缺失帧路径：跳过并记录。
    samples["broken_frames"] = write_manifest(
        "broken_frames",
        frames=[
            *_frames(0, 1),
            {"time_ms": 200, "path": broken_path},
            {"time_ms": 300, "path": "../../assets/frames/does_not_exist.png"},
            *_frames(4, 5),
        ],
        panel=PANEL,
        subtitles=[{"start_ms": 0, "end_ms": 500, "text": "坏帧不影响其他帧", "onset_ms": 0}],
    )
    return samples


def main() -> int:
    if ASSETS.exists():
        shutil.rmtree(ASSETS)
    if SAMPLES.exists():
        shutil.rmtree(SAMPLES)
    frames = write_frames()
    write_manifest("full_sequence", frames=frames, panel=PANEL, subtitles=[])
    samples = build_samples()
    print(f"已生成 {len(frames)} 帧样例：{ASSETS / 'frames'}")
    for name, path in sorted(samples.items()):
        print(f"  样例 {name}: {path}")
    print(f"  样例 full_sequence: {SAMPLES / 'full_sequence' / 'manifest.json'}")
    print("所有样例均为确定性生成，重复运行结果一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
