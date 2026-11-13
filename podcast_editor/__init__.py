"""播客单声道 16-bit PCM WAV 批量编辑工具（纯标准库）。

公开入口：
- EditorEngine: 编辑引擎（裁剪 / 拼接 / 音量调整）
- EditError / ErrorCode: 结构化业务错误
- load_wav / write_wav: WAV 读写
- build_report: 编辑报告生成
"""

from .engine import EditorEngine
from .errors import EditError, ErrorCode
from .report import build_report
from .wavio import load_wav, write_wav

__all__ = [
    "EditorEngine",
    "EditError",
    "ErrorCode",
    "load_wav",
    "write_wav",
    "build_report",
]

__version__ = "1.0.0"
