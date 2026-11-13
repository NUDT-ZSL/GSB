"""配音长录音切片与响度归一化工具（仅依赖 Python 标准库）。"""

from .cutter import (
    GAIN_MAX,
    GAIN_MIN,
    DEFAULT_FADE_SECONDS,
    DEFAULT_TARGET_DBFS,
    run,
    main,
)

__all__ = [
    "GAIN_MAX",
    "GAIN_MIN",
    "DEFAULT_FADE_SECONDS",
    "DEFAULT_TARGET_DBFS",
    "run",
    "main",
]
