"""数据模型与异常类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class PreviewError(Exception):
    """可映射到命令行退出码的错误基类。"""

    exit_code = 1

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InputFormatError(PreviewError):
    """输入文件缺失、非法 JSON 或结构不符合约定。退出码 2。"""

    exit_code = 2


class EnvironmentProblem(PreviewError):
    """运行环境问题（无 ffmpeg、无中文字体、输出目录不可写）。退出码 3。"""

    exit_code = 3


@dataclass(frozen=True)
class Shot:
    """镜头清单中的一条原始记录。"""

    shot_id: str
    start_ms: int
    end_ms: int
    input_index: int


@dataclass(frozen=True)
class PlacedShot:
    """放到时间轴上的镜头：effective 区间是实际参与渲染的区间。

    status:
        ok                  原始区间未与更早镜头重叠
        head_clamped        头部与更早镜头重叠，已按更早镜头的结束时刻切分
        fully_overlapped    整个区间被更早镜头覆盖，不产生任何帧
    """

    shot: Shot
    effective_start_ms: int
    effective_end_ms: int
    status: str


@dataclass(frozen=True)
class Gap:
    """素材空档：该时间段没有任何镜头。"""

    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class Conflict:
    """一对重叠镜头。earlier 是时间轴上切分点的依据。"""

    earlier: Shot
    later: Shot
    overlap_start_ms: int
    overlap_end_ms: int


@dataclass(frozen=True)
class Subtitle:
    """字幕表中的一条原始记录。"""

    index: int
    text: str
    start_ms: int
    end_ms: int
    shot_id: Optional[str]


@dataclass(frozen=True)
class ComposedSubtitle:
    """通过校验、会进入预览画面的字幕。"""

    subtitle: Subtitle
    shot_ids: tuple[str, ...]


@dataclass(frozen=True)
class UnfinishedItem:
    """未进入预览画面的字幕，reason 取固定枚举值。"""

    reason: str
    subtitle: Subtitle
    detail: str


@dataclass(frozen=True)
class FrameSpec:
    """单帧的绘制规格（渲染与逐帧核对都以它为准）。"""

    index: int
    time_ms: int
    kind: str  # "shot" | "gap"
    shot_id: Optional[str]
    segment_start_ms: int
    segment_end_ms: int
    palette_index: int
    subtitle_texts: tuple[str, ...]
