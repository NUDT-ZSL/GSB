"""工具使用的异常类型。"""


class CoverSelectorError(Exception):
    """可预期的输入或运行环境错误。"""


class ConfigError(CoverSelectorError):
    """配置无效。"""


class SetupError(CoverSelectorError):
    """目录、清单等运行前提不满足。"""


class FrameReadError(CoverSelectorError):
    """单帧无法读取或解码。"""
