"""三份输入的解析:SRT 字幕、转场清单 JSON、画幅配置 JSON。

每份输入独立解析,失败由调用方单独记录,互不影响。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

TIMESTAMP_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)

DEFAULT_SAFE_AREA_RATIO = 0.10  # 未提供安全区比例时取宽高各 10%


@dataclass
class Subtitle:
    order: int          # 在源文件中的顺序(0 起)
    index: int          # SRT 块声明的序号(无法解析时取 order+1)
    start_ms: int
    end_ms: int
    text: str


@dataclass
class Transition:
    start_frame: int
    end_frame: int
    description: str


@dataclass
class Canvas:
    width: int = 1920
    height: int = 1080
    fps: float = 25.0
    safe_area_ratio: Optional[float] = None  # None 表示未提供,取默认 10%
    font_size: int = 48
    line_height: int = 60
    font_path: Optional[str] = None

    @property
    def ratio(self) -> float:
        return DEFAULT_SAFE_AREA_RATIO if self.safe_area_ratio is None else self.safe_area_ratio

    @property
    def margin_x(self) -> int:
        return round(self.width * self.ratio)

    @property
    def margin_y(self) -> int:
        return round(self.height * self.ratio)


def _ts_to_ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def parse_srt(text: str) -> tuple[list[Subtitle], list[dict]]:
    """解析 SRT 文本,返回 (字幕列表, 块级错误列表)。坏块跳过并记录。"""
    subtitles: list[Subtitle] = []
    errors: list[dict] = []
    if not text.strip():
        return subtitles, errors
    blocks = re.split(r"\r?\n[ \t]*\r?\n", text.strip())
    for order, block in enumerate(blocks):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        ts_line = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ts_line is None:
            errors.append({"block_order": order, "error": "缺少时间轴行(-->)"})
            continue
        m = TIMESTAMP_RE.search(lines[ts_line])
        if not m:
            errors.append({"block_order": order, "error": f"时间轴格式错误: {lines[ts_line]!r}"})
            continue
        start_ms = _ts_to_ms(*m.group(1, 2, 3, 4))
        end_ms = _ts_to_ms(*m.group(5, 6, 7, 8))
        try:
            index = int(lines[0]) if ts_line > 0 else order + 1
        except ValueError:
            index = order + 1
        body = "\n".join(lines[ts_line + 1:]).strip()
        if not body:
            errors.append({"block_order": order, "error": "字幕正文为空"})
            continue
        subtitles.append(Subtitle(order=order, index=index,
                                  start_ms=start_ms, end_ms=end_ms, text=body))
    return subtitles, errors


def parse_transitions(text: str) -> list[Transition]:
    """解析转场清单 JSON。接受顶层数组或 {"transitions": [...]}。格式错误抛异常。"""
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("transitions", [])
    if not isinstance(data, list):
        raise ValueError("转场清单应为数组或含 transitions 字段的对象")
    transitions: list[Transition] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"转场第 {i} 项不是对象")
        try:
            start = int(item.get("start_frame", item.get("start")))
            end = int(item.get("end_frame", item.get("end")))
        except (TypeError, ValueError):
            raise ValueError(f"转场第 {i} 项帧区间缺失或非整数: {item!r}")
        transitions.append(Transition(
            start_frame=start, end_frame=end,
            description=str(item.get("description", item.get("desc", ""))),
        ))
    return transitions


def parse_canvas(text: str) -> Canvas:
    """解析画幅配置 JSON。格式错误抛异常。安全区比例可选。"""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("画幅配置应为 JSON 对象")
    canvas = Canvas(
        width=int(data["width"]),
        height=int(data["height"]),
        fps=float(data["fps"]),
        safe_area_ratio=(None if data.get("safe_area_ratio") is None
                         else float(data["safe_area_ratio"])),
        font_size=int(data.get("font_size", 48)),
        line_height=int(data.get("line_height", 60)),
        font_path=data.get("font_path"),
    )
    if canvas.width <= 0 or canvas.height <= 0:
        raise ValueError("画幅宽高必须为正数")
    if canvas.fps <= 0:
        raise ValueError("帧率必须为正数")
    if canvas.safe_area_ratio is not None and not (0 <= canvas.safe_area_ratio < 0.5):
        raise ValueError("安全区比例应在 [0, 0.5) 区间")
    return canvas
