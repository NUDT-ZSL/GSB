"""精灵图集动画接入核对工具。

读取动画描述 JSON 与 PNG 图集，逐动画核对：
  1. 帧网格宽高必须整除图集宽高，帧序号必须在网格内且不重复；
  2. 对相邻帧做逐像素差异检测（RGBA 四通道，含仅 alpha 变化），
     变化像素数超过阈值时输出脏矩形（图集像素坐标，原点左上角，
     以左下/右上两个角点表示）；
  3. 差异像素数未超过阈值时该帧视为无变化，脏矩形为空，
     拼接预览中保持前一帧内容；
  4. 循环动画额外检测末帧->首帧衔接，超阈值记为循环跳变；
  5. 为每个完整动画生成自左向右的拼接预览图；
  6. 输出 JSON 核对报告，整体状态区分 pass / jump / incomplete。

命令行：
    python sprite_check.py --atlas atlas.png --desc animations.json --out outdir
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops

MAX_ATLAS_SIZE = 2048

STATUS_PASS = "pass"              # 通过
STATUS_JUMP = "jump"              # 存在循环跳变
STATUS_INCOMPLETE = "incomplete"  # 不完整（描述非法或图集不可用）


# ---------------------------------------------------------------- 基础工具

def _sanitize_filename(name: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in name)
    return cleaned or "anim"


def load_atlas(path: Path):
    """加载图集，返回 (RGBA 图像或 None, 错误信息或 None)。"""
    try:
        img = Image.open(path)
        img.load()
    except FileNotFoundError:
        return None, f"atlas file not found: {path}"
    except Exception as exc:  # 无法解码（损坏文件等）
        return None, f"atlas cannot be decoded: {exc}"
    img = img.convert("RGBA")
    if img.width > MAX_ATLAS_SIZE or img.height > MAX_ATLAS_SIZE:
        return None, (
            f"atlas exceeds {MAX_ATLAS_SIZE}x{MAX_ATLAS_SIZE}: "
            f"{img.width}x{img.height}"
        )
    return img, None


def _changed_region(frame_a: Image.Image, frame_b: Image.Image):
    """比较两帧（同尺寸 RGBA），返回 (变化像素数, 局部 bbox 或 None)。

    bbox 为 Pillow 风格 (left, upper, right, bottom)，右/下为开边界。
    RGBA 任一通道不同即计为变化（含仅 alpha 差异）。
    """
    diff = ImageChops.difference(frame_a, frame_b)
    r, g, b, a = diff.split()
    mask = ImageChops.lighter(ImageChops.lighter(r, g), ImageChops.lighter(b, a))
    hist = mask.histogram()
    count = sum(hist[1:])
    if count == 0:
        return 0, None
    return count, mask.getbbox()


def _dirty_rect(origin_x: int, origin_y: int, bbox) -> dict:
    """把帧内局部 bbox 换算成图集坐标脏矩形（左下/右上，原点左上角）。"""
    left, upper, right, bottom = bbox
    x_min, x_max = origin_x + left, origin_x + right - 1
    y_min, y_max = origin_y + upper, origin_y + bottom - 1
    return {
        "lower_left": [x_min, y_max],
        "upper_right": [x_max, y_min],
    }


def _cell_origin(index: int, cols: int, fw: int, fh: int):
    return (index % cols) * fw, (index // cols) * fh


# ---------------------------------------------------------------- 动画核对

def check_animation(anim: dict, atlas, atlas_error, out_dir: Path,
                    used_names: set) -> dict:
    name = str(anim.get("name", "unnamed"))
    frames_idx = anim.get("frames", [])
    fw = anim.get("frame_width")
    fh = anim.get("frame_height")
    threshold = anim.get("diff_threshold", 0)
    loop = bool(anim.get("loop", False))

    errors: list = []
    result = {
        "name": name,
        "frame_count": len(frames_idx) if isinstance(frames_idx, list) else 0,
        "frame_width": fw,
        "frame_height": fh,
        "diff_threshold": threshold,
        "loop": loop,
        "status": STATUS_INCOMPLETE,
        "errors": errors,
        "frames": [],
        "loop_jump": None,
        "preview": None,
    }

    # ---- 描述自身校验（不依赖图集）----
    if not isinstance(fw, int) or not isinstance(fh, int) or fw <= 0 or fh <= 0:
        errors.append(f"invalid frame grid size: {fw}x{fh}")
        return result
    if not isinstance(frames_idx, list) or not frames_idx:
        errors.append("animation has no frames")
        return result
    if not isinstance(threshold, int) or threshold < 0:
        errors.append(f"invalid diff_threshold: {threshold}")
        return result

    bad_idx = [i for i in frames_idx if not isinstance(i, int) or i < 0]
    if bad_idx:
        errors.append(f"invalid frame indices: {bad_idx}")
    seen, dups = set(), set()
    for i in frames_idx:
        if i in seen:
            dups.add(i)
        seen.add(i)
    if dups:
        errors.append(f"duplicate frame indices: {sorted(dups)}")

    # ---- 图集可用性 ----
    if atlas is None:
        errors.append(f"atlas unavailable: {atlas_error}")
        return result

    aw, ah = atlas.size
    if aw % fw != 0 or ah % fh != 0:
        errors.append(f"frame grid {fw}x{fh} does not divide atlas {aw}x{ah}")
        return result
    cols = (aw // fw) * (ah // fh)
    oob = [i for i in frames_idx if isinstance(i, int) and i >= cols]
    if oob:
        errors.append(f"frame indices out of range (grid has {cols} cells): {oob}")
    if errors:
        return result  # 不完整：不生成该动画预览

    # ---- 逐帧差异检测 ----
    grid_cols = aw // fw
    cells = []
    for idx in frames_idx:
        ox, oy = _cell_origin(idx, grid_cols, fw, fh)
        cells.append((idx, ox, oy, atlas.crop((ox, oy, ox + fw, oy + fh))))

    n = len(cells)
    preview = Image.new("RGBA", (fw * n, fh))
    prev_slot = None
    for seq, (idx, ox, oy, cell) in enumerate(cells):
        if seq == 0:
            slot = cell
            entry = {"seq": 0, "index": idx, "first": True,
                     "changed_pixels": 0, "dirty_rect": None}
        else:
            count, bbox = _changed_region(cells[seq - 1][3], cell)
            if count > threshold:
                slot = cell
                entry = {"seq": seq, "index": idx, "first": False,
                         "changed_pixels": count,
                         "dirty_rect": _dirty_rect(ox, oy, bbox)}
            else:
                slot = prev_slot  # 无变化：预览保持前一帧内容
                entry = {"seq": seq, "index": idx, "first": False,
                         "changed_pixels": count, "dirty_rect": None}
        preview.paste(slot, (seq * fw, 0))
        prev_slot = slot
        result["frames"].append(entry)

    # ---- 循环衔接检测（末帧 -> 首帧）----
    status = STATUS_PASS
    if loop and n > 1:
        last_idx, _, _, last_cell = cells[-1]
        first_idx, fox, foy, first_cell = cells[0]
        count, bbox = _changed_region(last_cell, first_cell)
        if count > threshold:
            result["loop_jump"] = {
                "type": "loop_jump",
                "pair": [last_idx, first_idx],
                "changed_pixels": count,
                "dirty_rect": _dirty_rect(fox, foy, bbox),
            }
            status = STATUS_JUMP

    # ---- 拼接预览输出 ----
    base = _sanitize_filename(name)
    fname, k = f"{base}_preview.png", 1
    while fname in used_names:
        k += 1
        fname = f"{base}_preview_{k}.png"
    used_names.add(fname)
    preview.save(out_dir / fname, format="PNG")
    result["preview"] = fname
    result["status"] = status
    return result


# ---------------------------------------------------------------- 整体流程

def run(atlas_path: Path, desc_path: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    atlas, atlas_error = load_atlas(atlas_path)
    desc = json.loads(Path(desc_path).read_text(encoding="utf-8"))
    animations = desc.get("animations", [])

    report = {
        "tool": "sprite-check",
        "atlas": {
            "path": str(atlas_path),
            "available": atlas is not None,
            "error": atlas_error,
            "width": atlas.width if atlas else None,
            "height": atlas.height if atlas else None,
        },
        "description": str(desc_path),
        "animations": [],
        "summary": {"total": 0, "pass": 0, "jump": 0, "incomplete": 0},
    }

    used_names: set = set()
    for anim in animations:
        res = check_animation(anim, atlas, atlas_error, out_dir, used_names)
        report["animations"].append(res)
        report["summary"]["total"] += 1
        report["summary"][res["status"]] += 1
        print(f"[{res['status']:<10}] {res['name']}: "
              f"{res['frame_count']} frame(s)"
              + (f", preview={res['preview']}" if res["preview"] else "")
              + (f", errors: {'; '.join(res['errors'])}" if res["errors"] else ""))

    report_path = out_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"report written to {report_path}")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="核对精灵图集动画：帧差异脏矩形、循环跳变与拼接预览")
    parser.add_argument("--atlas", required=True, help="PNG 图集路径")
    parser.add_argument("--desc", required=True, help="动画描述 JSON 路径")
    parser.add_argument("--out", required=True, help="输出目录（不存在时自动创建）")
    args = parser.parse_args(argv)

    try:
        run(Path(args.atlas), Path(args.desc), Path(args.out))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
