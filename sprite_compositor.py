# -*- coding: utf-8 -*-
"""植被精灵场景合成预览工具。

读取美术交付的摆放清单 JSON，按清单顺序把精灵（草、花、树叶）合成到透明画布上，
并基于精灵 alpha 通道叠加柔和阴影，输出合成预览 PNG 与逐精灵报告 JSON。

用法:
    python sprite_compositor.py <清单.json>

退出码:
    0 - 正常执行（允许部分精灵未完成，详见报告）
    1 - 整体拒绝执行（清单非法 / 画布超限 / 输出目录不可写等）
    2 - 命令行参数错误
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter

MAX_CANVAS_SHORT_SIDE = 4096
PREVIEW_NAME = "preview.png"
REPORT_NAME = "report.json"


class ManifestError(Exception):
    """清单级错误：整体拒绝执行，不产生任何输出。"""


class SpriteSourceError(Exception):
    """单个精灵的源图问题（缺失 / 无法解码 / 尺寸不足）。"""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------- 清单读取与整体校验

def load_manifest(manifest_path):
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ManifestError(f"清单文件不存在: {manifest_path}")
    except json.JSONDecodeError as exc:
        raise ManifestError(f"清单 JSON 解析失败: {exc}")
    except OSError as exc:
        raise ManifestError(f"清单读取失败: {exc}")
    if not isinstance(data, dict):
        raise ManifestError("清单根节点必须是 JSON 对象")
    return data


def validate_canvas(manifest):
    """校验画布尺寸；短边超过 4096 时整体拒绝执行。"""
    canvas = manifest.get("canvas")
    if not isinstance(canvas, dict):
        raise ManifestError("清单缺少 canvas 对象")
    width, height = canvas.get("width"), canvas.get("height")
    if not _is_int(width) or not _is_int(height) or width <= 0 or height <= 0:
        raise ManifestError("画布宽高必须为正整数")
    short_side = min(width, height)
    if short_side > MAX_CANVAS_SHORT_SIDE:
        raise ManifestError(
            f"画布短边 {short_side} 超过上限 {MAX_CANVAS_SHORT_SIDE}，整体拒绝执行"
        )
    return width, height


def resolve_output_dir(manifest, manifest_path):
    output_dir = manifest.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ManifestError("清单缺少有效的 output_dir")
    out_dir = Path(output_dir)
    if not out_dir.is_absolute():
        out_dir = Path(manifest_path).resolve().parent / out_dir
    return out_dir


def ensure_writable(out_dir: Path):
    """输出目录不存在时自动创建；不可写时整体报错（在合成前调用，不产生半成品）。"""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ManifestError(f"输出目录不可写或无法创建: {out_dir} ({exc})")
    if not out_dir.is_dir():
        raise ManifestError(f"输出路径不是目录: {out_dir}")
    try:
        with tempfile.NamedTemporaryFile(dir=out_dir, prefix=".write_test_", delete=True):
            pass
    except OSError as exc:
        raise ManifestError(f"输出目录不可写: {out_dir} ({exc})")


# ---------------------------------------------------------------- 单精灵参数校验

def validate_sprite_params(cfg, canvas_w, canvas_h):
    """返回 None 表示合法，否则返回未完成原因。"""
    rect = cfg.get("rect")
    if not isinstance(rect, dict):
        return "参数非法: 摆放矩形缺失或不是对象"
    x, y = rect.get("x"), rect.get("y")
    w, h = rect.get("width"), rect.get("height")
    if not all(_is_int(v) for v in (x, y, w, h)):
        return "参数非法: 摆放矩形 x/y/width/height 必须为整数"
    if w <= 0 or h <= 0:
        return "参数非法: 摆放矩形宽高必须为正整数"

    shadow = cfg.get("shadow", {})
    if not isinstance(shadow, dict):
        return "参数非法: 阴影参数必须是对象"
    ox = shadow.get("offset_x", 0)
    oy = shadow.get("offset_y", 0)
    blur = shadow.get("blur_radius", 0)
    if not all(_is_int(v) for v in (ox, oy, blur)):
        return "参数非法: 阴影偏移量与模糊半径必须为整数"
    if ox < 0 or oy < 0 or blur < 0:
        return "参数非法: 阴影偏移量与模糊半径必须为非负整数"
    limit = min(canvas_w, canvas_h) // 4
    if ox > limit or oy > limit:
        return f"参数非法: 阴影偏移量超过画布短边四分之一({limit})"
    return None


# ---------------------------------------------------------------- 合成

def load_sprite(src_path: Path) -> Image.Image:
    if not src_path.is_file():
        raise SpriteSourceError(f"源图缺失: {src_path}")
    try:
        with Image.open(src_path) as img:
            img.load()
            return img.convert("RGBA")
    except SpriteSourceError:
        raise
    except Exception as exc:
        raise SpriteSourceError(f"源图无法解码: {src_path} ({type(exc).__name__}: {exc})")


def _composite_clipped(canvas: Image.Image, overlay: Image.Image, x: int, y: int):
    """把 overlay 合成到画布 (x, y) 处，越界部分裁掉、画布内内容保留。"""
    cw, ch = canvas.size
    ow, oh = overlay.size
    left, top = max(x, 0), max(y, 0)
    right, bottom = min(x + ow, cw), min(y + oh, ch)
    if right <= left or bottom <= top:
        return
    cropped = overlay.crop((left - x, top - y, right - x, bottom - y))
    canvas.alpha_composite(cropped, (left, top))


def process_sprite(canvas: Image.Image, cfg: dict, manifest_dir: Path, canvas_size):
    """处理单个精灵：缩放、生成柔和阴影并合成到画布。异常向上抛出由调用方记录。"""
    rect = cfg["rect"]
    x, y, w, h = rect["x"], rect["y"], rect["width"], rect["height"]
    shadow_cfg = cfg.get("shadow", {}) or {}

    source = cfg.get("source", cfg.get("path"))
    if not isinstance(source, str) or not source.strip():
        raise SpriteSourceError("源图路径缺失或不是字符串")
    src_path = Path(source)
    if not src_path.is_absolute():
        src_path = manifest_dir / src_path

    sprite = load_sprite(src_path)
    if sprite.width < w or sprite.height < h:
        raise SpriteSourceError(
            f"源图像素尺寸 {sprite.width}x{sprite.height} 小于摆放矩形 {w}x{h}，不放大合成"
        )
    if (sprite.width, sprite.height) != (w, h):
        sprite = sprite.resize((w, h), Image.LANCZOS)

    # 阴影：基于精灵 alpha 通道生成黑色剪影，再按模糊半径柔化，
    # 透明区域 alpha 为 0，不会出现矩形暗块。
    alpha = sprite.getchannel("A")
    shadow = Image.composite(
        Image.new("RGBA", (w, h), (0, 0, 0, 255)),
        Image.new("RGBA", (w, h), (0, 0, 0, 0)),
        alpha,
    )
    blur = shadow_cfg.get("blur_radius", 0)
    if blur > 0:
        shadow = shadow.filter(ImageFilter.GaussianBlur(blur))

    ox = shadow_cfg.get("offset_x", 0)
    oy = shadow_cfg.get("offset_y", 0)
    _composite_clipped(canvas, shadow, x + ox, y + oy)  # 阴影在下
    _composite_clipped(canvas, sprite, x, y)            # 精灵在上


# ---------------------------------------------------------------- 输出

def _atomic_write_bytes(target: Path, data: bytes):
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp_", suffix=target.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 主流程

def run(manifest_path):
    """执行合成，返回报告 dict。整体级错误抛出 ManifestError。"""
    manifest = load_manifest(manifest_path)
    canvas_w, canvas_h = validate_canvas(manifest)  # 画布超限在此拒绝，不触碰输出目录
    out_dir = resolve_output_dir(manifest, manifest_path)
    ensure_writable(out_dir)

    sprites = manifest.get("sprites")
    if not isinstance(sprites, list):
        raise ManifestError("清单缺少 sprites 数组")

    manifest_dir = Path(manifest_path).resolve().parent
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    entries = []
    completed = 0
    for index, cfg in enumerate(sprites):
        name = cfg.get("name") if isinstance(cfg, dict) else None
        if not isinstance(name, str) or not name:
            name = f"sprite_{index}"
        entry = {"name": name, "status": "failed", "preview": None, "reason": None}

        if not isinstance(cfg, dict):
            entry["reason"] = "精灵配置不是 JSON 对象"
        else:
            reason = validate_sprite_params(cfg, canvas_w, canvas_h)
            if reason is not None:
                entry["reason"] = reason
            else:
                try:
                    process_sprite(canvas, cfg, manifest_dir, (canvas_w, canvas_h))
                except SpriteSourceError as exc:
                    entry["reason"] = str(exc)
                except Exception as exc:  # 单精灵合成异常不影响其他精灵
                    entry["reason"] = f"合成过程异常: {type(exc).__name__}: {exc}"
                else:
                    entry["status"] = "completed"
                    entry["preview"] = PREVIEW_NAME  # 相对输出目录，正斜杠
                    completed += 1
        entries.append(entry)

    report = {
        "canvas": {"width": canvas_w, "height": canvas_h},
        "completed_count": completed,
        "total_count": len(entries),
        "sprites": entries,
    }

    import io
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    _atomic_write_bytes(out_dir / PREVIEW_NAME, buf.getvalue())
    _atomic_write_bytes(
        out_dir / REPORT_NAME,
        json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return report


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) != 2:
        print("用法: python sprite_compositor.py <清单.json>", file=sys.stderr)
        return 2
    try:
        report = run(argv[1])
    except ManifestError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(
        f"合成完成: {report['completed_count']}/{report['total_count']} 个精灵已完成，"
        f"预览与报告已写出。"
    )
    for entry in report["sprites"]:
        if entry["status"] != "completed":
            print(f"  未完成: {entry['name']} - {entry['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
