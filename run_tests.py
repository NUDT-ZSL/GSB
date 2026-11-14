"""测试入口：在 python -I 隔离模式下也能找到项目包。

运行:
    python -I run_tests.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_audit")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
