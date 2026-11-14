"""业务异常体系：所有错误都带可读中文信息，便于剪辑组直接定位问题。"""


class BeatAlignError(Exception):
    """本项目所有业务异常的基类。"""


class AudioReadError(BeatAlignError):
    """WAV 文件不存在、损坏或数据帧不完整，无法读取。"""


class UnsupportedAudioError(BeatAlignError):
    """WAV 可以解析，但采样率 / 位深 / 声道数不在支持范围内。"""


class ShotListError(BeatAlignError):
    """镜头清单 JSON 结构不合法。"""


class OutputNotWritableError(BeatAlignError):
    """预览图输出目录不存在且无法创建，或目录不可写。"""
