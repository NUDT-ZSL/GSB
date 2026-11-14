"""Deterministically generate the local sample descriptions and frame images.

    python tools/make_sample.py            # writes ./samples
    python tools/make_sample.py --out X    # writes X

Every byte is derived from constants in this file: no timestamps, no random
module, no external assets.  Re-running produces identical files.

The samples cover, on purpose:
  * frame order defined by the JSON array while file names are scrambled;
  * frames smaller and larger than the canvas, plus mixed alpha/RGB;
  * an "issues" description with a missing file, a corrupt PNG, a duplicated
    index, removed indices, and invalid (negative / non-numeric) durations;
  * a description with fps=0 that must produce a readable failure.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

CANVAS = (320, 180)
BACKGROUND = "#101820"
FPS = 24

# Deterministic, deliberately scrambled file names.  The export order comes
# only from the description array.
CLEAN_FILES = [
    "shot_k.png",
    "b_reader.png",
    "zeta_09.png",
    "aaa_start.png",
    "frame-0007.png",
    "take_p.png",
    "harbor.png",
    "m_0002.png",
    "zzz_close.png",
    "overture.png",
    "q_scene.png",
    "x_0011.png",
    "plate_c.png",
    "n_medium.png",
    "r_far.png",
    "s_hold.png",
    "t_fade.png",
    "u_end.png",
]

# Durations exercise different timeline lengths, including 0.
CLEAN_DURATIONS = [83, 125, 42, 167, 250, 0, 1000, 333, 41, 500, 125, 67, 250, 83, 42, 167, 100, 375]

# Per-frame source geometry and pixel mode.
#   (width, height, mode, transparent_corner, pattern)
CLEAN_GEOMETRY = [
    (320, 180, "RGBA", True, "gradient"),   # exact canvas, alpha
    (160, 90, "RGBA", True, "bars"),        # small, alpha
    (480, 270, "RGB", False, "grid"),       # large, no alpha
    (320, 180, "RGB", False, "rings"),      # exact canvas, no alpha
    (200, 120, "RGBA", False, "bars"),      # small, opaque alpha channel
    (640, 360, "RGB", False, "gradient"),   # large
    (320, 180, "RGBA", True, "grid"),
    (100, 200, "RGBA", True, "rings"),      # narrower but taller than canvas
    (300, 170, "RGB", False, "bars"),       # small by a hair
    (320, 180, "RGBA", True, "gradient"),
    (90, 60, "RGB", False, "grid"),         # tiny
    (321, 181, "RGBA", True, "rings"),      # just over canvas
    (320, 180, "RGB", False, "bars"),
    (240, 135, "RGBA", True, "grid"),
    (500, 90, "RGB", False, "gradient"),    # wide letterbox
    (180, 320, "RGBA", True, "bars"),       # tall portrait
    (320, 180, "RGBA", False, "rings"),
    (320, 180, "RGBA", True, "gradient"),
]


def _pattern(index: int, width: int, height: int, kind: str, draw: ImageDraw.ImageDraw) -> None:
    """Draw a deterministic, index-dependent pattern (no font dependency)."""
    base_hue = (index * 37) % 256
    for y in range(height):
        if kind == "gradient":
            value = (base_hue + y * 90 // max(1, height)) % 256
            draw.line([(0, y), (width, y)], fill=(value, (base_hue * 2 + 40) % 256, 255 - value, 255))
        elif kind == "bars":
            value = (base_hue + y // 8 * 24) % 256
            draw.line([(0, y), (width, y)], fill=((value * 3) % 256, 60, 200 - value // 2, 255))
        elif kind == "grid":
            draw.line([(0, y), (width, y)], fill=(20, 30 + (base_hue % 40), 45, 255))
        else:
            draw.line([(0, y), (width, y)], fill=(15, 25, 35, 255))

    if kind == "bars":
        bar_count = 3 + index % 4
        for n in range(bar_count):
            x0 = (n * width) // (bar_count + 1)
            draw.rectangle(
                [x0, height // 3, x0 + max(2, width // 18), height - height // 3],
                fill=(240 - n * 20, 180 - index * 3, 60 + n * 25, 255),
            )
    elif kind == "grid":
        step_x = max(8, width // 8)
        step_y = max(8, height // 6)
        for x in range(step_x, width, step_x):
            draw.line([(x, 0), (x, height)], fill=(70, 120, 150, 255), width=1)
        for y in range(step_y, height, step_y):
            draw.line([(0, y), (width, y)], fill=(70, 120, 150, 255), width=1)
    elif kind == "rings":
        cx, cy = width // 2, height // 2
        radius = max(4, min(width, height) // 2 - 2)
        for n in range(5):
            box = [cx - radius + n * 5, cy - radius + n * 5, cx + radius - n * 5, cy + radius - n * 5]
            if box[2] <= box[0] or box[3] <= box[1]:
                break
            draw.ellipse(box, outline=((base_hue + n * 50) % 256, 200 - n * 20, 120, 255), width=2)

    # A binary marker block whose bit pattern encodes index + 1, so a human
    # can tell frames apart at a glance and tests can identify them.
    marker = index + 1
    block = max(3, min(width, height) // 24)
    for bit in range(8):
        if marker & (1 << bit):
            x0 = 4 + bit * (block + 2)
            y0 = height - block - 4
            draw.rectangle([x0, y0, x0 + block, y0 + block], fill=(250, 250, 250, 255))


def build_frame(index: int, width: int, height: int, mode: str, transparent: bool, kind: str) -> Image.Image:
    background = (0, 0, 0, 0) if transparent else (0, 0, 0, 255)
    image = Image.new("RGBA", (width, height), background)
    draw = ImageDraw.Draw(image)
    _pattern(index, width, height, kind, draw)

    if transparent:
        # A hard-edged transparent corner (RGBA paste, no blending).
        cut_w = max(4, width // 4)
        cut_h = max(4, height // 4)
        draw.rectangle([0, 0, cut_w, cut_h], fill=(0, 0, 0, 0))
        # And one semi-transparent stripe.
        draw.rectangle(
            [0, height - cut_h, cut_w, height],
            fill=(255, 0, 128, 96),
        )
    if mode == "RGB":
        return image.convert("RGB")
    return image


def _save_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=6)
    return buffer.getvalue()


def write_description(path: Path, frames: list[dict], *, fps=FPS, frame_count=None) -> None:
    payload = {
        "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
        "background": BACKGROUND,
        "fps": fps,
        "frame_count": len(frames) if frame_count is None else frame_count,
        "frames": frames,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def make_clean(out: Path) -> Path:
    root = out / "clean_storyboard"
    frames_dir = root / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict] = []
    for index, (name, duration, geometry) in enumerate(
        zip(CLEAN_FILES, CLEAN_DURATIONS, CLEAN_GEOMETRY)
    ):
        width, height, mode, transparent, kind = geometry
        image = build_frame(index, width, height, mode, transparent, kind)
        (frames_dir / name).write_bytes(_save_png(image))
        frames.append({"index": index, "file": f"frames/{name}", "duration_ms": duration})
    write_description(root / "description.json", frames)
    return root / "description.json"


def make_issues(out: Path, clean_spec: Path) -> Path:
    """Copy the clean sample and inject realistic faults."""
    root = out / "storyboard_issues"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    shutil.copytree(clean_spec.parent / "frames", root / "frames")

    frames = json.loads(clean_spec.read_text(encoding="utf-8"))["frames"]

    # 1) A deleted frame file (missing_file).
    (root / "frames" / CLEAN_FILES[6]).unlink()
    # 2) A corrupt PNG (valid signature, truncated body).
    corrupt = (root / "frames" / CLEAN_FILES[9]).read_bytes()
    (root / "frames" / CLEAN_FILES[9]).write_bytes(corrupt[: len(corrupt) // 3])
    # 3) Negative duration for one frame (invalid frame, batch continues).
    frames[5]["duration_ms"] = -1
    # 4) Non-numeric duration for another frame.
    frames[13]["duration_ms"] = "not-a-number"
    # 5) Remove index 3 and duplicate index 4 in its place -> gap 3, duplicate 4.
    frames[3] = {
        "index": 4,
        "file": f"frames/{CLEAN_FILES[4]}",
        "duration_ms": 167,
    }
    # 6) Declared count stays 18 so the reports show the mismatch vs entries.
    payload = {
        "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
        "background": BACKGROUND,
        "fps": FPS,
        "frame_count": 18,
        "frames": frames,
    }
    (root / "description.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return root / "description.json"


def make_invalid_fps(out: Path) -> Path:
    root = out / "invalid_fps"
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
        "background": BACKGROUND,
        "fps": 0,
        "frames": [
            {"index": 0, "file": "frames/shot.png", "duration_ms": 100},
            {"index": 1, "file": "frames/shot2.png", "duration_ms": 100},
        ],
    }
    (root / "description.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return root / "description.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic sample data.")
    parser.add_argument("--out", default="samples", help="output directory (default: samples)")
    args = parser.parse_args()
    out = Path(args.out)

    clean = make_clean(out)
    issues = make_issues(out, clean)
    invalid = make_invalid_fps(out)
    print(f"clean:   {clean}")
    print(f"issues:  {issues}")
    print(f"invalid: {invalid}")
    total = sum(1 for _ in (out / "clean_storyboard" / "frames").iterdir())
    print(f"frames per sample: {total} (limit 200)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
