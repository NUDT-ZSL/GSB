# -*- coding: utf-8 -*-
"""程序入口。

用法：
    python -m app.main [动画JSON路径]

不传路径时显示"文件不可用 / 选择文件"界面（需求 9）。
快捷键：空格 播放/暂停，←/→ 逐帧，↑/↓ 切换骨骼，
        C 切换曲线视图，R 重新加载，E 导出报告，Esc 退出。
"""
from __future__ import annotations

import os
import sys

# 兼容 python -I app/main.py 直接执行（此时项目根不在 sys.path）
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.ui import App
else:
    from .ui import App


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    app = App(path)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
