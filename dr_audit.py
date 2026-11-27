#!/usr/bin/env python3
"""命令行脚本入口，支持 `python3 dr_audit.py ...` 直接运行。"""

import sys

from dr_audit.cli import main

if __name__ == "__main__":
    sys.exit(main())

