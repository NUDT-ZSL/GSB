"""端到端演示入口：python run_demo.py

等价于 python -m token_guard.cli demo，但可从项目根目录直接双击/命令行运行。
使用 token_guard.samples 中的确定性样本，不访问网络、不读取外部文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from token_guard.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["demo"]))
