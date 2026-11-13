"""检测工具显式入口。

以 `python -I main.py ...` 方式运行：隔离模式（-I）屏蔽用户站点包与环境变量注入，
同时本文件把项目根目录显式加入 sys.path，保证 order_sec_check 包可被导入，
不依赖 cwd 或 PYTHONPATH。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from order_sec_check.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
