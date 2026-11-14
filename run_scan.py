#!/usr/bin/env python
"""隔离模式入口脚本。

用法（注意 -I 必须位于脚本路径之前）：
    python -I run_scan.py scan <扫描目录> -o <报告路径>
    python -I run_scan.py generate <样本目录>
    python -I run_scan.py compare <修复前报告> <修复后报告> -o <比对结果>

-I 关闭用户站点包与环境变量注入，并跳过当前目录隐式导入，
适合处理来源不可信的样本目录；脚本会自行把仓库根加入 sys.path。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanner.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
