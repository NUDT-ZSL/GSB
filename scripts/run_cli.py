"""CLI 入口垫片：支持在隔离模式（python -I）下从任意工作目录直接运行。

等价于 ``python -m podcast_editor.cli``，但不依赖 cwd 自动注入 sys.path
（``-I`` 模式下该注入会被禁用）。参数原样透传给 CLI。
"""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from podcast_editor.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
