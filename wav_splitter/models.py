"""数据模型：CSV 标记、音频信息与处理结果。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# 报告中使用的状态常量（中文，与任务验收口径一致）
STATUS_SUCCESS = "成功"
STATUS_OUT_OF_RANGE = "越界"
STATUS_INVALID_INTERVAL = "无效区间"
STATUS_OVERLAP = "重叠"

ALL_STATUSES = (
    STATUS_SUCCESS,
    STATUS_OUT_OF_RANGE,
    STATUS_INVALID_INTERVAL,
    STATUS_OVERLAP,
)


@dataclass(frozen=True)
class Marker:
    """CSV 中的一行标记。

    ``start`` / ``end`` 保留 :class:`~decimal.Decimal` 高精度值，
    避免浮点解析（如 1.2345）在毫秒量化时产生误差；
    ``raw_start`` / ``raw_end`` 保留 CSV 原始文本，报告中原样回显。
    """

    segment_id: str          # 片段编号（原样保留大小写）
    start: Decimal           # 起始秒（解析后的数值）
    end: Decimal             # 结束秒（解析后的数值）
    raw_start: str           # CSV 原始起始时间文本
    raw_end: str             # CSV 原始结束时间文本
    row_number: int          # CSV 物理行号（含表头行，从 1 开始）


@dataclass(frozen=True)
class WavInfo:
    """已校验的单声道 PCM WAV 头部信息。"""

    path: str
    channels: int            # 必须为 1
    sample_width: int        # 字节数（1/2/3/4）
    frame_rate: int          # 采样率 Hz
    frame_count: int         # 总采样帧数（单声道下即采样点数）
    comptype: str            # 压缩类型，PCM 为 "NONE"
    compname: str

    @property
    def duration_seconds(self) -> float:
        """音频总时长（秒）。"""
        return self.frame_count / self.frame_rate


@dataclass
class SegmentResult:
    """单个标记的处理结果，最终序列化为 JSON 报告中的一条。"""

    segment_id: str
    row_number: int
    raw_start: str
    raw_end: str
    status: str
    error: str | None                 # 成功时为 None
    output_file: str | None = None    # 成功时的导出文件名（仅文件名）
    start_sample: int | None = None   # 成功时的首采样点索引
    end_sample: int | None = None     # 成功时的结束采样点索引（不含）
