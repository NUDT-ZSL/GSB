"""本地 WAV 剪辑拼接工具（仅依赖 Python 标准库）。

模块职责：
- 读取 16-bit PCM WAV 源文件与 JSON 剪辑方案；
- 按方案顺序裁剪、应用线性淡入/淡出与增益后拼接输出 WAV；
- 单个片段失败不影响其他片段，跳过原因写入 JSON 报告。
"""

from .cli import main

__all__ = ["main"]
