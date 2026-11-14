# 使 tests 目录下的测试可以直接 import 项目根目录的 sprite_check。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
