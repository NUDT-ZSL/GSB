"""支持 `python -m skill_cards` 直接调用（含 `python -I` 隔离模式）。"""
import os
import sys

# 隔离模式（-I）下脚本目录不会自动进入 sys.path，显式补上包根目录
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from skill_cards.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
