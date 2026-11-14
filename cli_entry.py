"""隔离模式（python -I）下的命令行入口。

用法：
    python -I cli_entry.py --srt <path> --config <path> --out <dir>

脚本自身位于项目根，sys.path[0] 即根目录，-I 隔离模式下仍可导入本项目包，
同时不会从 cwd 加载任何第三方 json.py 之类的植入模块。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from srt_safety.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
