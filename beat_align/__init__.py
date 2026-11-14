"""宣传片鼓点对齐工具（离线）。

模块组成::

    audio    读取并校验 WAV（采样率 / 位深 / 声道数）
    detect   能量突变法检出鼓点，合并过近的重复检出
    shots    读取镜头清单并按累计时长推导原始起点
    align    把每个镜头起点对齐到最近鼓点
    preview  用 Pillow 生成切换预览图
    report   输出节奏核查结果 JSON
    pipeline 串联整条离线流水线
    cli      命令行入口
"""

from .errors import (
    AudioReadError,
    BeatAlignError,
    OutputNotWritableError,
    ShotListError,
    UnsupportedAudioError,
)

__all__ = [
    "BeatAlignError",
    "AudioReadError",
    "UnsupportedAudioError",
    "ShotListError",
    "OutputNotWritableError",
]
__version__ = "1.0.0"
