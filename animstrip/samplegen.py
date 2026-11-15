"""确定性样本生成器：固定种子生成帧 PNG 与动画清单，无需外部素材。

覆盖场景：
- anim_alpha     时长配平，含全透明/半透明像素，子目录相对路径
- anim_mixsize   帧高不一致（垂直居中），帧在清单中乱序（按序号排序验证）
- anim_loop      绝对路径写法，时长配平
- anim_drift     帧停留之和偏离目标（差值 +50 毫秒）
- anim_missing   引用的一帧文件缺失，不出条带
- anim_corrupt   引用的文件无法解码，不出条带
- anim_bad_dur   目标时长为 0，结构校验拒绝
- anim_bad_dur_neg 目标时长为负数，结构校验拒绝
- anim_dup_index 帧序号重复，结构校验拒绝
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from PIL import Image

SEED = 20261115

# 动画 ID -> (目标时长 ms, [(宽, 高, 停留 ms), ...])
BALANCED_LAYOUTS: dict[str, tuple[int, list[tuple[int, int, int]]]] = {
    "anim_alpha": (300, [(48, 48, 100), (48, 48, 100), (48, 48, 100)]),
    "anim_mixsize": (260, [(64, 32, 90), (32, 64, 80), (48, 40, 90)]),
    "anim_loop": (240, [(40, 40, 80), (40, 40, 80), (40, 40, 80)]),
    "anim_drift": (400, [(48, 48, 120), (48, 48, 120), (48, 48, 100), (48, 48, 110)]),
}


def render_frame(frame_id: int, width: int, height: int) -> Image.Image:
    """按固定数学规律渲染 RGBA 帧，结果完全确定。

    alpha 分布：(x+y)%7==0 的像素全透明（alpha=0），
    左侧三分之一半透明（alpha=128），其余不透明（alpha=255）。
    """
    seed_rng = _seeded_rng(frame_id)
    base = (
        seed_rng.randrange(40, 256),
        seed_rng.randrange(40, 256),
        seed_rng.randrange(40, 256),
    )
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = img.load()
    for y in range(height):
        for x in range(width):
            if (x + y) % 7 == 0:
                alpha = 0
            elif x < max(1, width // 3):
                alpha = 128
            else:
                alpha = 255
            # RGB 随坐标做确定性微调，保证每帧像素可区分
            pixels[x, y] = (
                (base[0] + x * 3) % 256,
                (base[1] + y * 5) % 256,
                (base[2] + (x + y) * 7) % 256,
                alpha,
            )
    return img


def _seeded_rng(frame_id: int):
    import random

    return random.Random(SEED + frame_id)


def _frame_ref(rel_path: str) -> dict:
    return {"image": rel_path}


def generate(sample_dir: str | Path) -> Path:
    """在 sample_dir 下确定性重建整套样本，返回清单路径。"""
    sample_dir = Path(sample_dir).resolve()
    if sample_dir.exists():
        shutil.rmtree(sample_dir)
    frames_root = sample_dir / "frames"
    frames_root.mkdir(parents=True, exist_ok=True)

    frame_counter = 0
    animations: list[dict] = []

    # 1) 正常/偏差动画（子目录相对路径）
    for name, (target, layout) in BALANCED_LAYOUTS.items():
        anim_dir = frames_root / name
        anim_dir.mkdir(parents=True, exist_ok=True)
        frame_entries = []
        for order, (width, height, duration) in enumerate(layout):
            img = render_frame(frame_counter, width, height)
            frame_counter += 1
            rel = f"frames/{name}/f{order}.png"
            img.save(sample_dir / rel, format="PNG", optimize=False)
            frame_entries.append(
                {"index": order, "image": rel, "duration_ms": duration}
            )
        if name == "anim_mixsize":
            # 清单内故意乱序，验证合成时按 index 排序
            frame_entries = [frame_entries[2], frame_entries[0], frame_entries[1]]
        animations.append(
            {
                "animation_id": name,
                "target_duration_ms": target,
                "frames": frame_entries,
            }
        )

    # 2) anim_loop 改用绝对路径写法
    loop_entry = next(a for a in animations if a["animation_id"] == "anim_loop")
    for entry in loop_entry["frames"]:
        entry["image"] = str((sample_dir / entry["image"]).resolve())

    # 3) 缺帧动画：三帧只写两帧，中间帧缺失
    missing_dir = frames_root / "anim_missing"
    missing_dir.mkdir(parents=True, exist_ok=True)
    missing_frames = []
    for order in range(3):
        rel = f"frames/anim_missing/f{order}.png"
        missing_frames.append(
            {"index": order, "image": rel, "duration_ms": 100}
        )
        if order == 1:
            continue  # 故意不生成该文件
        render_frame(frame_counter, 36, 36).save(
            sample_dir / rel, format="PNG", optimize=False
        )
        frame_counter += 1
    animations.append(
        {
            "animation_id": "anim_missing",
            "target_duration_ms": 300,
            "frames": missing_frames,
        }
    )

    # 4) 损坏文件动画：写入非 PNG 字节
    corrupt_dir = frames_root / "anim_corrupt"
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    broken_rel = "frames/anim_corrupt/broken.png"
    (sample_dir / broken_rel).write_bytes(b"not-a-valid-png-" + bytes([SEED % 256]))
    animations.append(
        {
            "animation_id": "anim_corrupt",
            "target_duration_ms": 100,
            "frames": [
                {"index": 0, "image": broken_rel, "duration_ms": 100},
            ],
        }
    )

    # 5) 被结构校验拒绝的条目
    animations.append(
        {
            "animation_id": "anim_bad_dur",
            "target_duration_ms": 0,
            "frames": [
                {"index": 0, "image": "frames/anim_alpha/f0.png", "duration_ms": 50},
            ],
        }
    )
    animations.append(
        {
            "animation_id": "anim_bad_dur_neg",
            "target_duration_ms": -5,
            "frames": [
                {"index": 0, "image": "frames/anim_alpha/f0.png", "duration_ms": 50},
            ],
        }
    )
    animations.append(
        {
            "animation_id": "anim_dup_index",
            "target_duration_ms": 200,
            "frames": [
                {"index": 1, "image": "frames/anim_alpha/f0.png", "duration_ms": 100},
                {"index": 1, "image": "frames/anim_alpha/f1.png", "duration_ms": 100},
            ],
        }
    )

    manifest = {"animations": animations}
    manifest_path = sample_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return manifest_path
