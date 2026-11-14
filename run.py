"""根级启动器，便于在 `python -I` 隔离模式下运行：

    python -I run.py --manifest samples/skills_manifest.json --out "输出 目录/检视卡"

普通模式下也可使用：python -m skill_cards --manifest ... --out ...
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from skill_cards.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
