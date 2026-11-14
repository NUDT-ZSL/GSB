"""Nine-patch (九宫格) texture slicer and preview renderer.

The manifest is a JSON file.  Paths inside the manifest are resolved relative
to the directory that contains the manifest file.

Manifest form (a top-level list is accepted as well)::

    {
      "textures": [
        {
          "texture_name": "button_blue",
          "image_path": "textures/button_blue.png",
          "left": 14, "right": 14, "top": 14, "bottom": 14,
          "target_width": 240,
          "target_height": 96
        }
      ]
    }

For convenience the aliases ``name``/``image`` and
``target_width``/``width`` (likewise for height) are accepted too.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from PIL import Image, UnidentifiedImageError

# Image.Resampling arrived in Pillow 9.1; Pillow 9.0 only has the constants on
# Image itself, so fall back gracefully.
RESAMPLING = getattr(Image, "Resampling", Image)
BILINEAR = RESAMPLING.BILINEAR

SUCCESS = "成功"
FAILURE = "失败"
IMAGE_READ_ERROR = "图片读取失败"
MARGIN_ERROR = "边距非法"

RESULT_FILENAME = "result.json"


class ManifestError(Exception):
    """The manifest itself could not be read."""


class EntryError(Exception):
    """A single manifest entry is invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TextureSpec:
    texture_name: str
    image_path: str
    left: int
    right: int
    top: int
    bottom: int
    target_width: int
    target_height: int


@dataclass(frozen=True)
class RenderPlan:
    """Effective (possibly compressed) margins and preview dimensions."""

    src_width: int
    src_height: int
    target_width: int
    target_height: int
    left: int
    right: int
    top: int
    bottom: int
    compressed: bool

    @property
    def center_width(self) -> int:
        return self.target_width - self.left - self.right

    @property
    def center_height(self) -> int:
        return self.target_height - self.top - self.bottom


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------

_NAME_KEYS = ("texture_name", "name", "textureName", "texture", "纹理名")
_IMAGE_KEYS = ("image_path", "image", "path", "imagePath", "图片路径")
_LEFT_KEYS = ("left", "left_margin", "margin_left", "左边距")
_RIGHT_KEYS = ("right", "right_margin", "margin_right", "右边距")
_TOP_KEYS = ("top", "top_margin", "margin_top", "上边距")
_BOTTOM_KEYS = ("bottom", "bottom_margin", "margin_bottom", "下边距")
_WIDTH_KEYS = ("target_width", "targetWidth", "width", "目标宽")
_HEIGHT_KEYS = ("target_height", "targetHeight", "height", "目标高")
_LIST_KEYS = ("textures", "items", "entries", "tiles", "list", "纹理列表")


def _pick(mapping: dict[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _require_text(mapping: dict[str, Any], keys: Sequence[str], label: str) -> str:
    value = _pick(mapping, keys)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise EntryError(f"清单条目非法: 缺少{label}")
    return str(value)


def _as_int(value: Any, label: str, error_prefix: str) -> int:
    """Convert a JSON number to an int, rejecting bools and fractions."""

    if isinstance(value, bool):
        raise EntryError(f"{error_prefix}: {label}必须为整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 10)
        except ValueError:
            pass
    raise EntryError(f"{error_prefix}: {label}必须为整数")


def parse_entry(raw: Any) -> TextureSpec:
    if not isinstance(raw, dict):
        raise EntryError("清单条目非法: 条目必须是JSON对象")

    name = _require_text(raw, _NAME_KEYS, "纹理名")
    image_path = _require_text(raw, _IMAGE_KEYS, "图片路径")

    margins: dict[str, int] = {}
    for label, keys in (
        ("左边距", _LEFT_KEYS),
        ("右边距", _RIGHT_KEYS),
        ("上边距", _TOP_KEYS),
        ("下边距", _BOTTOM_KEYS),
    ):
        value = _pick(raw, keys)
        if value is None:
            raise EntryError(f"{MARGIN_ERROR}: 缺少{label}")
        margins[label] = _as_int(value, label, MARGIN_ERROR)

    for label, value in margins.items():
        if value < 0:
            raise EntryError(f"{MARGIN_ERROR}: {label}不能为负数({value})")

    width_value = _pick(raw, _WIDTH_KEYS)
    height_value = _pick(raw, _HEIGHT_KEYS)
    if width_value is None or height_value is None:
        raise EntryError("目标尺寸非法: 缺少目标宽高")
    width = _as_int(width_value, "目标宽度", "目标尺寸非法")
    height = _as_int(height_value, "目标高度", "目标尺寸非法")
    if width <= 0 or height <= 0:
        raise EntryError(f"目标尺寸非法: 目标宽高必须为正整数({width}x{height})")

    return TextureSpec(
        texture_name=name,
        image_path=image_path,
        left=margins["左边距"],
        right=margins["右边距"],
        top=margins["上边距"],
        bottom=margins["下边距"],
        target_width=width,
        target_height=height,
    )


def load_manifest(manifest_path: str | Path) -> list[Any]:
    path = Path(manifest_path)
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ManifestError(f"清单文件不存在: {path}") from exc
    except OSError as exc:
        raise ManifestError(f"清单文件读取失败: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"清单JSON解析失败: {exc}") from exc

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
        # A single-entry manifest is unusual but harmless to support.
        if any(key in data for key in _NAME_KEYS):
            return [data]
    raise ManifestError("清单JSON格式非法: 顶层应为数组或含textures数组的对象")


# ---------------------------------------------------------------------------
# Slicing / rendering
# ---------------------------------------------------------------------------

def split_nine_patch(
    image: Image.Image,
    left: int,
    right: int,
    top: int,
    bottom: int,
) -> list[list[Image.Image]]:
    """Return the nine source crops in row-major order.

    A zero margin produces a zero-sized crop in that direction.  Source
    boundaries are exactly the supplied margins.
    """

    width, height = image.size
    xs = (0, left, width - right, width)
    ys = (0, top, height - bottom, height)
    return [
        [image.crop((xs[col], ys[row], xs[col + 1], ys[row + 1])) for col in range(3)]
        for row in range(3)
    ]


def _shrink_pair(first: int, second: int, target: int) -> tuple[int, int, bool]:
    """Compress two opposing margins so their sum fits ``target``.

    Non-zero margins keep at least one pixel whenever the target allows it.
    A zero margin stays zero: that direction has no border in the preview.
    """

    total = first + second
    if total <= target:
        return first, second, False

    nonzero = int(first > 0) + int(second > 0)
    if nonzero == 0:  # Cannot happen when total > target >= 1.
        return first, second, False

    if target < nonzero:
        # Mathematically impossible to keep both sides: keep the larger one.
        if first >= second:
            return 1, 0, True
        return 0, 1, True

    budget = target - nonzero
    total_f = float(total)
    extra_first = budget * first / total_f
    extra_second = budget * second / total_f
    add_first = math.floor(extra_first)
    add_second = math.floor(extra_second)
    remaining = budget - add_first - add_second

    if remaining > 0:
        if (extra_first - add_first) >= (extra_second - add_second):
            add_first += 1
        else:
            add_second += 1
        remaining -= 1
    if remaining > 0:
        if first > 0:
            add_first += remaining
        else:
            add_second += remaining

    new_first = (1 if first > 0 else 0) + add_first
    new_second = (1 if second > 0 else 0) + add_second
    # Clamp defensively; both values should already be <= their originals.
    new_first = min(new_first, first) if first else 0
    new_second = min(new_second, second) if second else 0
    if new_first + new_second > target:
        excess = new_first + new_second - target
        if new_first >= new_second:
            new_first -= excess
        else:
            new_second -= excess
    return new_first, new_second, True


def make_render_plan(spec: TextureSpec, source_size: tuple[int, int]) -> RenderPlan:
    src_width, src_height = source_size
    if spec.left + spec.right > src_width:
        raise EntryError(
            f"{MARGIN_ERROR}: 左右边距之和({spec.left + spec.right})"
            f"超过图片宽度({src_width})"
        )
    if spec.top + spec.bottom > src_height:
        raise EntryError(
            f"{MARGIN_ERROR}: 上下边距之和({spec.top + spec.bottom})"
            f"超过图片高度({src_height})"
        )

    left, right, compressed_h = _shrink_pair(
        spec.left, spec.right, spec.target_width
    )
    top, bottom, compressed_v = _shrink_pair(
        spec.top, spec.bottom, spec.target_height
    )
    return RenderPlan(
        src_width=src_width,
        src_height=src_height,
        target_width=spec.target_width,
        target_height=spec.target_height,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
        compressed=compressed_h or compressed_v,
    )


def _preview_mode(image: Image.Image) -> str:
    # Keep RGB/RGBA/L/LA pixel values byte-for-byte.  Palette and other modes
    # are normalised to RGBA so transparency survives.
    if image.mode in ("RGB", "RGBA", "L", "LA"):
        return image.mode
    return "RGBA"


def _paste_resized(
    canvas: Image.Image,
    source: Image.Image,
    source_box: tuple[int, int, int, int],
    destination_size: tuple[int, int],
    destination_position: tuple[int, int],
) -> None:
    dest_width, dest_height = destination_size
    if dest_width <= 0 or dest_height <= 0:
        return
    src_width, src_height = source.size
    x0, y0, x1, y1 = source_box
    x0 = max(0, min(x0, src_width))
    x1 = max(0, min(x1, src_width))
    y0 = max(0, min(y0, src_height))
    y1 = max(0, min(y1, src_height))

    # A degenerate axis (e.g. a source centre of zero width because the
    # margins cover the whole image) still has to fill the destination.  Replicate
    # the single nearest source column/row instead of leaving a transparent hole.
    if x1 <= x0:
        if src_width <= 0:
            return
        x0 = min(max(0, x0 - 1), src_width - 1)
        x1 = x0 + 1
    if y1 <= y0:
        if src_height <= 0:
            return
        y0 = min(max(0, y0 - 1), src_height - 1)
        y1 = y0 + 1

    region = source.crop((x0, y0, x1, y1))
    if region.size != (dest_width, dest_height):
        region = region.resize((dest_width, dest_height), resample=BILINEAR)
    canvas.paste(region, destination_position)


def render_preview(image: Image.Image, plan: RenderPlan) -> Image.Image:
    """Render a nine-patch preview.

    Corners are copied at their native size; the four edges are stretched in
    one direction and the centre in both.  ``plan.left/right/top/bottom`` are
    the effective margins, so they may be smaller than the manifest values
    when compression was required.
    """

    mode = _preview_mode(image)
    source = image if image.mode == mode else image.convert(mode)
    width, height = plan.target_width, plan.target_height
    left, right = plan.left, plan.right
    top, bottom = plan.top, plan.bottom
    center_w = plan.center_width
    center_h = plan.center_height
    src_w, src_h = plan.src_width, plan.src_height

    canvas = Image.new(mode, (width, height))

    # Four corners: paste the source crop without scaling.
    corners = (
        ((0, 0, left, top), (0, 0)),
        ((src_w - right, 0, src_w, top), (width - right, 0)),
        ((0, src_h - bottom, left, src_h), (0, height - bottom)),
        ((src_w - right, src_h - bottom, src_w, src_h), (width - right, height - bottom)),
    )
    for box, position in corners:
        if box[2] > box[0] and box[3] > box[1]:
            canvas.paste(source.crop(box), position)

    # Four edges: stretch in one direction only.
    _paste_resized(
        canvas,
        source,
        (left, 0, src_w - right, top),
        (center_w, top),
        (left, 0),
    )
    _paste_resized(
        canvas,
        source,
        (left, src_h - bottom, src_w - right, src_h),
        (center_w, bottom),
        (left, height - bottom),
    )
    _paste_resized(
        canvas,
        source,
        (0, top, left, src_h - bottom),
        (left, center_h),
        (0, top),
    )
    _paste_resized(
        canvas,
        source,
        (src_w - right, top, src_w, src_h - bottom),
        (right, center_h),
        (width - right, top),
    )

    # Centre: stretch in both directions.
    _paste_resized(
        canvas,
        source,
        (left, top, src_w - right, src_h - bottom),
        (center_w, center_h),
        (left, top),
    )
    return canvas


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def _preview_name(texture_name: str) -> str:
    # Avoid path traversal / accidentally creating subdirectories while keeping
    # ordinary (including non-ASCII) names untouched.
    safe = texture_name.replace("/", "_").replace("\\", "_").strip()
    if safe in ("", ".", ".."):
        safe = "texture"
    return f"{safe}_preview.png"


def _read_image(path: Path) -> Image.Image:
    try:
        with Image.open(path) as opened:
            opened.load()
            # Copy out of the file-backed object so the context manager can
            # close the file descriptor safely.
            return opened.copy()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise EntryError(f"{IMAGE_READ_ERROR}: {path} ({exc})") from exc
    except Exception as exc:  # Pillow has several decoder-specific errors.
        raise EntryError(f"{IMAGE_READ_ERROR}: {path} ({exc})") from exc


def process_item(
    raw: Any,
    manifest_dir: Path,
    output_dir: Path,
    index: int,
) -> tuple[dict[str, Any], str | None]:
    """Process one manifest entry.

    Returns ``(result_record, console_text)``.  ``console_text`` is either
    ``已生成: ...`` or ``跳过: ...``.
    """

    fallback_name = f"item_{index + 1}"
    preview_path: Path | None = None

    def failure(reason: str, name: str | None = None) -> tuple[dict[str, Any], str]:
        name = fallback_name if name is None else name
        record = {
            "texture_name": name,
            "status": FAILURE,
            "compressed": None,
            "failure_reason": reason,
        }
        return record, f"跳过: {name} - {reason}"

    # Extract a best-effort name before full validation so console output and
    # result JSON identify the broken entry.
    if isinstance(raw, dict):
        candidate = _pick(raw, _NAME_KEYS)
        if isinstance(candidate, str) and candidate.strip():
            fallback_name = candidate
    try:
        spec = parse_entry(raw)
    except EntryError as exc:
        return failure(exc.reason)

    name = spec.texture_name
    preview_path = output_dir / _preview_name(name)
    # A failed run must not leave a stale preview from an earlier run.
    try:
        preview_path.unlink(missing_ok=True)
    except OSError:
        pass

    image_path = Path(spec.image_path).expanduser()
    if not image_path.is_absolute():
        image_path = manifest_dir / image_path
    try:
        image = _read_image(image_path)
    except EntryError as exc:
        return failure(exc.reason, name)

    try:
        plan = make_render_plan(spec, image.size)
    except EntryError as exc:
        return failure(exc.reason, name)

    try:
        preview = render_preview(image, plan)
        preview.save(preview_path, format="PNG")
    except Exception as exc:
        try:
            preview_path.unlink(missing_ok=True)
        except OSError:
            pass
        return failure(f"预览生成失败: {exc}", name)

    record = {
        "texture_name": name,
        "status": SUCCESS,
        "compressed": plan.compressed,
        "failure_reason": None,
    }
    return record, f"已生成: {name}"


def process_manifest(
    manifest_path: str | Path,
    output_dir: str | Path,
    result_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    manifest_path = Path(manifest_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    entries = load_manifest(manifest_path)
    manifest_dir = manifest_path.parent
    results: list[dict[str, Any]] = []
    success_count = 0
    failure_count = 0

    for index, raw in enumerate(entries):
        record, message = process_item(raw, manifest_dir, output_dir, index)
        results.append(record)
        print(message)
        if record["status"] == SUCCESS:
            success_count += 1
        else:
            failure_count += 1

    destination = Path(result_path) if result_path else output_dir / RESULT_FILENAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"完成: 成功{success_count}项, 失败{failure_count}项")
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> "argparse.ArgumentParser":
    import argparse

    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="按九宫格边距切割纹理并渲染目标尺寸预览。",
    )
    parser.add_argument(
        "manifest_positional",
        nargs="?",
        metavar="清单路径",
        help="输入清单JSON路径（默认: samples/manifest.json）",
    )
    parser.add_argument(
        "output_positional",
        nargs="?",
        metavar="输出目录",
        help="输出目录（默认: output）",
    )
    parser.add_argument(
        "-i",
        "--input",
        "--manifest",
        dest="manifest",
        help="输入清单JSON路径",
    )
    parser.add_argument(
        "-o",
        "--output",
        "--out",
        dest="output",
        help="输出目录，不存在时自动创建",
    )
    parser.add_argument(
        "--result",
        dest="result",
        help="结果JSON路径（默认: <输出目录>/result.json）",
    )
    parser.set_defaults(base_dir=base_dir)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    manifest = args.manifest or args.manifest_positional or str(
        args.base_dir / "samples" / "manifest.json"
    )
    output = args.output or args.output_positional or str(
        args.base_dir / "output"
    )

    try:
        process_manifest(manifest, output, args.result)
    except ManifestError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"错误: 输出失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
