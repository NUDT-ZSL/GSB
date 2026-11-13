"""游戏音效批处理工具包：WAV 静音裁剪 / 淡入淡出 / 增益 + JSON 元数据。

仅依赖 Python 3.10+ 标准库（wave / struct / json / pathlib 等）。
"""

from .config import PipelineConfig, ConfigError, load_config, DEFAULT_CONFIG
from .wav_io import WavData, WavFormatError, read_wav, write_wav
from .processing import trim_silence, apply_fade, apply_gain
from .pipeline import process_directory, ProcessReport

__all__ = [
    "PipelineConfig",
    "ConfigError",
    "load_config",
    "DEFAULT_CONFIG",
    "WavData",
    "WavFormatError",
    "read_wav",
    "write_wav",
    "trim_silence",
    "apply_fade",
    "apply_gain",
    "process_directory",
    "ProcessReport",
]

__version__ = "1.0.0"
