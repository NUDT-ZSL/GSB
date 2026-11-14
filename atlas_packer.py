"""离线纹理图集打包预览器。

按图集配置 JSON 将本地小图排入固定尺寸画布，生成图集 PNG 与落位清单 JSON。
确定性输出：同一配置重复运行，图集 PNG 与清单 JSON 逐字节一致。

用法:
    python atlas_packer.py --config <配置JSON路径> --out <输出目录>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from PIL import Image

STATUS_PLACED = "placed"
STATUS_MISSING_SOURCE = "missing_source"
STATUS_OVERFLOW = "overflow"

ATLAS_FILENAME = "atlas.png"
MANIFEST_FILENAME = "manifest.json"


class ConfigError(Exception):
    """配置 JSON 格式错误或内容非法。"""


def load_config(config_path: str) -> dict:
    """读取并校验配置，非法时抛出 ConfigError。"""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"配置文件不存在: {config_path}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"配置 JSON 解析失败: {e}")
    except OSError as e:
        raise ConfigError(f"配置文件读取失败: {e}")

    if not isinstance(cfg, dict):
        raise ConfigError("配置根节点必须是 JSON 对象")

    canvas = cfg.get("canvas")
    if not isinstance(canvas, dict):
        raise ConfigError("缺少必需字段: canvas (对象, 含 width/height)")
    width, height = canvas.get("width"), canvas.get("height")
    for label, v in (("canvas.width", width), ("canvas.height", height)):
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ConfigError(f"{label} 必须是正整数, 当前值: {v!r}")

    bleed = cfg.get("bleed", 0)
    if not isinstance(bleed, int) or isinstance(bleed, bool) or bleed < 0:
        raise ConfigError(f"bleed 必须是非负整数, 当前值: {bleed!r}")

    sprites = cfg.get("sprites")
    if not isinstance(sprites, list):
        raise ConfigError("缺少必需字段: sprites (数组)")
    for i, sp in enumerate(sprites):
        if not isinstance(sp, dict):
            raise ConfigError(f"sprites[{i}] 必须是对象")
        if not isinstance(sp.get("name"), str) or not sp["name"]:
            raise ConfigError(f"sprites[{i}].name 必须是非空字符串")
        if not isinstance(sp.get("src"), str) or not sp["src"]:
            raise ConfigError(f"sprites[{i}].src 必须是非空字符串")
        if "rotate" in sp and not isinstance(sp["rotate"], bool):
            raise ConfigError(f"sprites[{i}].rotate 必须是布尔值")

    return {
        "canvas_width": width,
        "canvas_height": height,
        "bleed": bleed,
        "sprites": [
            {
                "name": sp["name"],
                "src": sp["src"],
                "rotate": bool(sp.get("rotate", False)),
            }
            for sp in sprites
        ],
    }


def resolve_src(src: str, config_dir: str) -> str:
    """源路径相对配置文件所在目录解析。"""
    if os.path.isabs(src):
        return src
    return os.path.normpath(os.path.join(config_dir, src))


def has_transparency(img: Image.Image) -> bool:
    """判断 RGBA 图像是否含非全不透明像素。"""
    alpha = img.getchannel("A")
    lo, _hi = alpha.getextrema()
    return lo < 255


def make_bled_image(img: Image.Image, bleed: int) -> Image.Image:
    """将 RGBA 小图四边向外扩展 bleed 像素的同色(含 alpha)边缘。

    透明像素按原样复制，不填充为不透明色。
    """
    if bleed <= 0:
        return img.copy()
    w, h = img.size
    out = Image.new("RGBA", (w + 2 * bleed, h + 2 * bleed))
    out.paste(img, (bleed, bleed))
    nearest = Image.Resampling.NEAREST
    # 上下边: 复制边缘行
    out.paste(img.crop((0, 0, w, 1)).resize((w, bleed), nearest), (bleed, 0))
    out.paste(img.crop((0, h - 1, w, h)).resize((w, bleed), nearest), (bleed, bleed + h))
    # 左右边: 复制边缘列
    out.paste(img.crop((0, 0, 1, h)).resize((bleed, h), nearest), (0, bleed))
    out.paste(img.crop((w - 1, 0, w, h)).resize((bleed, h), nearest), (bleed + w, bleed))
    # 四角: 复制角像素
    out.paste(img.crop((0, 0, 1, 1)).resize((bleed, bleed), nearest), (0, 0))
    out.paste(img.crop((w - 1, 0, w, 1)).resize((bleed, bleed), nearest), (bleed + w, 0))
    out.paste(img.crop((0, h - 1, 1, h)).resize((bleed, bleed), nearest), (0, bleed + h))
    out.paste(
        img.crop((w - 1, h - 1, w, h)).resize((bleed, bleed), nearest),
        (bleed + w, bleed + h),
    )
    return out


class ShelfPacker:
    """确定性货架式排布: 按配置顺序逐行放置, 占位含出血边距。

    占位足迹 = 落位矩形 + 四边 bleed, 保证出血区不与任何相邻落位矩形重叠,
    且出血扩展不超出画布。
    """

    def __init__(self, canvas_w: int, canvas_h: int, bleed: int):
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        self.bleed = bleed
        self.cursor_x = 0
        self.cursor_y = 0
        self.shelf_h = 0

    def place(self, w: int, h: int) -> tuple[int, int] | None:
        """返回落位矩形左上角 (x, y); 放不下返回 None。"""
        fw = w + 2 * self.bleed
        fh = h + 2 * self.bleed
        if fw > self.canvas_w or fh > self.canvas_h:
            return None
        if self.cursor_x + fw > self.canvas_w:
            # 换行
            self.cursor_x = 0
            self.cursor_y += self.shelf_h
            self.shelf_h = 0
        if self.cursor_y + fh > self.canvas_h:
            return None
        x = self.cursor_x + self.bleed
        y = self.cursor_y + self.bleed
        self.cursor_x += fw
        self.shelf_h = max(self.shelf_h, fh)
        return (x, y)


def pack(cfg: dict, config_dir: str) -> tuple[Image.Image, dict]:
    """执行打包, 返回 (图集图像, 清单字典)。"""
    cw, ch, bleed = cfg["canvas_width"], cfg["canvas_height"], cfg["bleed"]
    atlas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    packer = ShelfPacker(cw, ch, bleed)
    entries = []

    for sp in cfg["sprites"]:
        src_abs = resolve_src(sp["src"], config_dir)
        entry = {
            "name": sp["name"],
            "src": sp["src"],
            "rotated": sp["rotate"],
            "rect": None,
            "has_alpha": None,
            "status": None,
        }
        entries.append(entry)

        try:
            with Image.open(src_abs) as im:
                img = im.convert("RGBA")
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            # 缺失源或无法解码: 标记未处理, 继续打包其余小图
            entry["status"] = STATUS_MISSING_SOURCE
            continue

        entry["has_alpha"] = has_transparency(img)

        if sp["rotate"]:
            # 顺时针旋转 90 度
            img = img.transpose(Image.Transpose.ROTATE_270)

        w, h = img.size
        pos = packer.place(w, h)
        if pos is None:
            entry["status"] = STATUS_OVERFLOW
            continue

        x, y = pos
        bled = make_bled_image(img, bleed)
        # 无 mask 粘贴: 整体替换目标区域, 保留源 alpha(透明像素保持透明)
        atlas.paste(bled, (x - bleed, y - bleed))
        entry["rect"] = {"x": x, "y": y, "width": w, "height": h}
        entry["status"] = STATUS_PLACED

    manifest = {
        "canvas": {"width": cw, "height": ch},
        "bleed": bleed,
        "sprites": entries,
    }
    return atlas, manifest


def ensure_writable_dir(out_dir: str) -> None:
    """确认输出目录可创建且可写, 否则抛 OSError。"""
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.isdir(out_dir):
        raise OSError(f"输出路径不是目录: {out_dir}")
    fd, probe = tempfile.mkstemp(prefix=".write_probe_", dir=out_dir)
    try:
        os.write(fd, b"")
    finally:
        os.close(fd)
        os.remove(probe)


def write_outputs(atlas: Image.Image, manifest: dict, out_dir: str) -> tuple[str, str]:
    """先写临时文件再原子替换, 避免残缺输出。返回 (图集路径, 清单路径)。"""
    atlas_path = os.path.join(out_dir, ATLAS_FILENAME)
    manifest_path = os.path.join(out_dir, MANIFEST_FILENAME)

    fd, tmp_png = tempfile.mkstemp(suffix=".png", dir=out_dir)
    os.close(fd)
    try:
        atlas.save(tmp_png, format="PNG")
        os.replace(tmp_png, atlas_path)
    except BaseException:
        if os.path.exists(tmp_png):
            os.remove(tmp_png)
        raise

    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_json = tempfile.mkstemp(suffix=".json", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_json, manifest_path)
    except BaseException:
        if os.path.exists(tmp_json):
            os.remove(tmp_json)
        raise

    return atlas_path, manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="atlas_packer",
        description="离线纹理图集打包预览器: 按配置JSON打包小图, 输出图集PNG与落位清单JSON。",
    )
    parser.add_argument("--config", required=True, help="图集配置 JSON 路径")
    parser.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2

    try:
        ensure_writable_dir(args.out)
    except OSError as e:
        print(f"输出目录不可写: {args.out}: {e}", file=sys.stderr)
        return 3

    config_dir = os.path.dirname(os.path.abspath(args.config))
    atlas, manifest = pack(cfg, config_dir)

    try:
        atlas_path, manifest_path = write_outputs(atlas, manifest, args.out)
    except OSError as e:
        print(f"输出写入失败: {e}", file=sys.stderr)
        return 3

    placed = sum(1 for s in manifest["sprites"] if s["status"] == STATUS_PLACED)
    missing = sum(1 for s in manifest["sprites"] if s["status"] == STATUS_MISSING_SOURCE)
    overflow = sum(1 for s in manifest["sprites"] if s["status"] == STATUS_OVERFLOW)
    print(f"图集: {atlas_path}")
    print(f"清单: {manifest_path}")
    print(f"已落位 {placed} 张, 缺失源 {missing} 张, 溢出 {overflow} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
