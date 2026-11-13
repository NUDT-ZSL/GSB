"""测试入口：支持隔离模式运行（python -I run_tests.py）。

显式将项目根目录加入 sys.path，不依赖 cwd 或 PYTHONPATH，
避免 -I 模式下模块无法导入的问题。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_end_to_end")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
