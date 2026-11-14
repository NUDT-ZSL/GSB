"""确定性生成业务样本：候选帧图片 + 清单 JSON。

同机重复运行产生字节一致的图片与清单，便于核对与重建。
覆盖场景：
- V001 三候选（横向三格总宽校验）        - V002 两候选
- V003 单候选                           - V004 文案避让保护框（缩字号）
- V005 极端样本（回退排版）              - V006 长文案自动折行+缩字号
- V007 混入损坏帧（占位+原因）           - V008 候选全部不可用
- V009 候选分辨率参差                    - V010 超宽画幅帧等比适配
- V011 无文案/无保护框
- V012 画布尺寸非法（跳过）              - V013 候选列表为空（跳过）
清单顺序故意打乱，用于验证结果 JSON 按视频ID排序。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _ensure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

_FONT_PATH = r"C:\Windows\Fonts\simhei.ttf"

_PALETTE = [
    (52, 96, 168),
    (168, 62, 96),
    (54, 130, 110),
    (160, 110, 40),
    (110, 60, 150),
    (170, 70, 50),
]


def _seed_rng(video_id: str, index: int) -> random.Random:
    digest = f"{video_id}:{index}".encode("utf-8")
    seed = int.from_bytes(digest, "little") & 0xFFFFFFFF
    return random.Random(seed)


def _vertical_gradient(width: int, height: int, base: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (width, height))
    px = img.load()
    # 高光上限 210，保证帧内不出现白系像素（白色仅用于封面文案）
    top = tuple(min(210, c + 50) for c in base)
    for y in range(height):
        ratio = y / max(1, height - 1)
        row = tuple(round(top[i] * (1 - ratio) + base[i] * ratio) for i in range(3))
        for x in range(width):
            px[x, y] = row
    return img


def make_frame(
    path: Path,
    video_id: str,
    index: int,
    width: int,
    height: int,
    subject_box: tuple[int, int, int, int] | None,
) -> None:
    """生成单张候选帧：渐变底 + 确定性几何装饰 + 主体色块 + 帧标签。"""
    rng = _seed_rng(video_id, index)
    color = _PALETTE[int.from_bytes(video_id.encode(), "little") % len(_PALETTE)]
    img = _vertical_gradient(width, height, color)
    draw = ImageDraw.Draw(img)

    # 确定性装饰元素（颜色上限 215，避免与白色文案混淆）
    for _ in range(10):
        x0 = rng.randint(0, width)
        y0 = rng.randint(0, height)
        radius = rng.randint(width // 25, width // 6)
        fill = tuple(rng.randint(70, 215) for _ in range(3))
        draw.ellipse([x0, y0, x0 + radius, y0 + radius], outline=fill, width=3)

    # 主体色块（与清单保护框位置一致）
    if subject_box is not None:
        x, y, w, h = subject_box
        draw.rectangle([x, y, x + w, y + h], fill=(245, 200, 120), outline=(120, 70, 10), width=4)

    # 帧标签放在画面右上角（深色底 + 灰色字，不使用白色），
    # 避开左上角候选序号徽标与顶部文案
    label = f"{video_id}-C{index}"
    try:
        font = ImageFont.truetype(_FONT_PATH, max(16, width // 16))
    except OSError:
        font = ImageFont.load_default()
    label_w = draw.textlength(label, font=font)
    ly = round(height * 0.16)
    draw.rectangle(
        [width - label_w - 26, ly, width - 10, ly + font.size + 20],
        fill=(20, 20, 20),
    )
    draw.text((width - label_w - 18, ly + 8), label, font=font, fill=(200, 200, 200))

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")


def make_corrupt(path: Path) -> None:
    """写入带 JPEG 头的非图像字节，Pillow 解码必然失败。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\xff\xd8\xff\xe0NOT-A-REAL-IMAGE-" + b"x" * 128 + b"\xff\xd9"
    )


def build_samples(root: Path) -> Path:
    frames_dir = root / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    # video_id: (画布, 候选分辨率列表, 文案, 保护框, min, max)
    specs: dict[str, dict] = {
        "V001": {
            "canvas": (1080, 1920),
            "sizes": [(1080, 1920), (900, 1600), (720, 1280)],
            "copy": "夏日大促 爆款直降",
            "box": (140, 760, 800, 760),
            "min": 40, "max": 96,
        },
        "V002": {
            "canvas": (1080, 1920),
            "sizes": [(1080, 1920), (810, 1440)],
            "copy": "新品上市",
            "box": (120, 700, 840, 900),
            "min": 40, "max": 88,
        },
        "V003": {
            "canvas": (720, 1280),
            "sizes": [(720, 1280)],
            "copy": "限时秒杀",
            "box": (90, 520, 540, 560),
            "min": 32, "max": 72,
        },
        "V004": {
            # 保护框上沿压入文案区，文案需缩字号才能避开
            "canvas": (1080, 1920),
            "sizes": [(1080, 1920), (1080, 1920), (960, 1707)],
            "copy": "清凉一夏 冰点优惠",
            "box": (60, 120, 960, 1200),
            "min": 40, "max": 96,
        },
        "V005": {
            # 保护框几乎占满画面区，最小字号仍相交 -> 回退排版
            "canvas": (1080, 1920),
            "sizes": [(1080, 1920), (900, 1600)],
            "copy": "全场五折 仅此一天",
            "box": (40, 60, 1000, 1580),
            "min": 40, "max": 96,
        },
        "V006": {
            # 长文案自动折行，且需要缩字号避让
            "canvas": (1080, 1920),
            "sizes": [(1080, 1920), (1080, 1920)],
            "copy": "超长文案测试：新人下载注册即领千元大礼包还有机会抽取限定周边好礼",
            "box": (80, 260, 920, 1100),
            "min": 36, "max": 90,
        },
        "V007": {
            # 第2张损坏、第3张缺失（清单仍列出路径）
            "canvas": (1080, 1920),
            "sizes": [(1080, 1920), None, None],
            "copy": "周末放价",
            "box": (150, 700, 780, 800),
            "min": 40, "max": 88,
        },
        "V008": {
            # 两张候选：一张损坏、一张缺失，全部不可用
            "canvas": (1080, 1920),
            "sizes": [None, None],
            "copy": "会员专场",
            "box": (150, 700, 780, 800),
            "min": 40, "max": 88,
        },
        "V009": {
            # 候选分辨率参差，等比适配进画面区
            "canvas": (1080, 1920),
            "sizes": [(640, 1136), (1080, 2400), (480, 854)],
            "copy": "高刷直屏",
            "box": (160, 680, 760, 820),
            "min": 40, "max": 84,
        },
        "V010": {
            # 超宽画幅帧，验证 contain 等比缩放
            "canvas": (1080, 1920),
            "sizes": [(1600, 900)],
            "copy": "全景大片",
            "box": None,
            "min": 40, "max": 84,
        },
        "V011": {
            "canvas": (720, 1280),
            "sizes": [(720, 1280), (720, 1280)],
            "copy": "",
            "box": None,
            "min": 32, "max": 72,
        },
    }

    entries: list[dict] = []
    for video_id, spec in specs.items():
        cw, ch = spec["canvas"]
        candidates: list[str] = []
        for idx, size in enumerate(spec["sizes"], start=1):
            if size is None:
                if video_id == "V007" and idx == 2:
                    rel = f"frames/{video_id}_c{idx}.jpg"
                    make_corrupt(root / rel)
                elif video_id == "V008" and idx == 1:
                    rel = f"frames/{video_id}_c{idx}.jpg"
                    make_corrupt(root / rel)
                else:
                    # 缺失文件：不写入任何内容
                    rel = f"frames/{video_id}_c{idx}.png"
            else:
                rel = f"frames/{video_id}_c{idx}.png"
                box = None
                if spec["box"] is not None:
                    bx, by, bw, bh = spec["box"]
                    sx, sy = size[0] / cw, size[1] / ch
                    box = (round(bx * sx), round(by * sy), round(bw * sx), round(bh * sy))
                make_frame(root / rel, video_id, idx, size[0], size[1], box)
            candidates.append(rel)

        entry = {
            "video_id": video_id,
            "canvas": {"width": cw, "height": ch},
            "candidates": candidates,
            "copy_text": spec["copy"],
            "min_font_size": spec["min"],
            "max_font_size": spec["max"],
        }
        if spec["box"] is not None:
            bx, by, bw, bh = spec["box"]
            entry["protection_box"] = {"x": bx, "y": by, "width": bw, "height": bh}
        entries.append(entry)

    # 跳过场景
    entries.append({
        "video_id": "V012",
        "canvas": {"width": 0, "height": 1920},
        "candidates": ["frames/V012_c1.png"],
        "copy_text": "非法画布",
        "protection_box": {"x": 0, "y": 0, "width": 100, "height": 100},
        "min_font_size": 40,
        "max_font_size": 88,
    })
    entries.append({
        "video_id": "V013",
        "canvas": {"width": 1080, "height": 1920},
        "candidates": [],
        "copy_text": "空候选",
        "min_font_size": 40,
        "max_font_size": 88,
    })

    # 确定性打乱清单顺序（验证输出按视频ID排序）
    shuffle_rng = random.Random(20261115)
    shuffle_rng.shuffle(entries)

    manifest = {"font": _FONT_PATH, "entries": entries}
    manifest_path = root / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="确定性生成封面预览任务样本")
    parser.add_argument("--out", default=None, help="样本输出目录（默认 ./samples）")
    args = parser.parse_args(argv)
    _ensure_utf8_console()
    root = Path(args.out).resolve() if args.out else Path.cwd() / "samples"
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = build_samples(root)
    frame_count = len(list((root / "frames").glob("*")))
    print(f"样本目录：{root}")
    print(f"候选帧文件数：{frame_count}")
    print(f"清单：{manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
