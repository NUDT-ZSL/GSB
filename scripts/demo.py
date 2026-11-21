"""演示入口：python3 scripts/demo.py（跨版本迁移 + 改写失效传播）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from asr_gov.demo import main

if __name__ == "__main__":
    raise SystemExit(main())
