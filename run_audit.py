"""隔离模式启动器（Python 3.10 兼容）。

背景：任务要求样例 JSON 为不可信数据。若把数据目录放进 cwd 或 sys.path，
同目录下被植入的 json.py 等模块会被标准库 import 误加载。
本启动器以 `python -I` 运行（隔离模式：忽略 PYTHONPATH、用户 site 包与 cwd），
再把本文件所在目录（工具包目录）显式注入 sys.path，兼顾安全与可移植。

用法：
    python -I run_audit.py <镜头清单.json> <字幕清单.json> [--csv ...] [--png ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

# -I 模式下 cwd 与 PYTHONPATH 均不在 sys.path 中，这里只加入可信的工具根目录
TOOL_ROOT = Path(__file__).resolve().parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from subtitle_audit.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
