"""数据结构与清单（manifest）加载、校验。

清单 JSON 结构::

    {
      "font": "可选，字体文件绝对/相对路径",
      "entries": [
        {
          "video_id": "V001",
          "canvas": {"width": 1080, "height": 1920},
          "candidates": ["frames/v001_1.png", ...],
          "copy_text": "封面文案",
          "protection_box": {"x": 120, "y": 600, "width": 840, "height": 900},
          "min_font_size": 40,
          "max_font_size": 96
        }
      ]
    }

所有候选帧路径相对于清单文件所在目录解析。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rect:
    """画布坐标下的矩形。"""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def intersects(self, other: "Rect", padding: int = 2) -> bool:
        """两个矩形是否相交（可带外扩像素，避免贴边渲染造成视觉压线）。"""
        return not (
            self.right + padding <= other.x
            or other.right + padding <= self.x
            or self.bottom + padding <= other.y
            or other.bottom + padding <= self.y
        )


@dataclass
class Candidate:
    """单个候选帧。"""

    path: Path                 # 解析后的绝对路径
    raw_path: str              # 清单中的原始路径字符串
    available: bool = True
    reason: str = ""           # 不可用原因（missing / decode_error）


@dataclass
class VideoEntry:
    """清单中一个可处理的视频条目。"""

    video_id: str
    canvas_width: int
    canvas_height: int
    candidates: list[Candidate]
    copy_text: str
    protection_box: Rect | None
    min_font_size: int
    max_font_size: int
    bar_height: int
    order: int                 # 在清单中的原始顺序


@dataclass
class SkippedEntry:
    """因清单数据非法而跳过的条目。"""

    video_id: str
    reason: str
    order: int


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _parse_box(raw: object) -> Rect | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("protection_box 必须是对象")
    coords = {}
    for key in ("x", "y", "width", "height"):
        num = _as_int(raw.get(key))
        if num is None or num < 0:
            raise ValueError(f"protection_box.{key} 必须为非负整数")
        coords[key] = num
    return Rect(**coords)


def load_manifest(manifest_path: str | Path) -> tuple[list[VideoEntry], list[SkippedEntry], Path | None]:
    """加载并校验清单。

    返回 (可处理条目, 跳过条目, 清单指定字体路径)。
    非法条目不会中断整批处理，而是进入跳过列表。
    """
    manifest_path = Path(manifest_path).resolve()
    base_dir = manifest_path.parent
    with manifest_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("清单根对象必须包含 entries 数组")

    font_path: Path | None = None
    if data.get("font"):
        font_path = Path(str(data["font"]))
        if not font_path.is_absolute():
            font_path = (base_dir / font_path).resolve()

    entries: list[VideoEntry] = []
    skipped: list[SkippedEntry] = []

    for order, raw_entry in enumerate(data["entries"]):
        video_id = str(raw_entry.get("video_id", "")).strip() if isinstance(raw_entry, dict) else ""
        label = video_id or f"第{order + 1}项"

        if not isinstance(raw_entry, dict):
            skipped.append(SkippedEntry(label, "条目不是 JSON 对象", order))
            continue

        # 画布尺寸校验
        canvas = raw_entry.get("canvas")
        if not isinstance(canvas, dict):
            skipped.append(SkippedEntry(label, "画布尺寸非法：缺少 canvas 对象", order))
            continue
        cw = _as_int(canvas.get("width"))
        ch = _as_int(canvas.get("height"))
        if cw is None or ch is None or cw <= 0 or ch <= 0:
            skipped.append(SkippedEntry(label, "画布尺寸非法：width/height 必须为正整数", order))
            continue

        # 候选列表校验
        raw_candidates = raw_entry.get("candidates")
        if not isinstance(raw_candidates, list) or len(raw_candidates) == 0:
            skipped.append(SkippedEntry(label, "候选列表为空", order))
            continue

        # 保护框校验（非法保护框按数据问题跳过）
        try:
            box = _parse_box(raw_entry.get("protection_box"))
        except ValueError as exc:
            skipped.append(SkippedEntry(label, f"保护框非法：{exc}", order))
            continue
        if box is not None and (box.right > cw or box.bottom > ch):
            skipped.append(SkippedEntry(label, "保护框非法：超出画布范围", order))
            continue

        min_size = _as_int(raw_entry.get("min_font_size")) or 40
        max_size = _as_int(raw_entry.get("max_font_size")) or 96
        if min_size <= 0 or max_size < min_size:
            skipped.append(SkippedEntry(label, "字号配置非法：min/max 取值无效", order))
            continue
        bar_height = _as_int(raw_entry.get("bar_height")) or max(160, min_size * 3)

        candidates: list[Candidate] = []
        for item in raw_candidates[:3]:  # 每个视频最多 3 张候选
            raw_path = str(item)
            resolved = Path(raw_path)
            if not resolved.is_absolute():
                resolved = (base_dir / resolved).resolve()
            candidates.append(Candidate(path=resolved, raw_path=raw_path))

        entries.append(
            VideoEntry(
                video_id=video_id or label,
                canvas_width=cw,
                canvas_height=ch,
                candidates=candidates,
                copy_text=str(raw_entry.get("copy_text", "")).strip(),
                protection_box=box,
                min_font_size=min_size,
                max_font_size=max_size,
                bar_height=bar_height,
                order=order,
            )
        )

    return entries, skipped, font_path
