# -*- coding: utf-8 -*-
"""项目根入口：任意工作目录下均可运行，参数与 `python -m scanner` 完全一致。

示例：
  python pt_scan.py                                   # 无参：扫描当前目录+默认配置
  python pt_scan.py --dir samples/vulnerable --report out/r.json
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scanner.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
