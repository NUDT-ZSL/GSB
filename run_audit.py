"""命令行入口：在 python -I 隔离模式下也能找到项目包。

示例:
    python -I run_audit.py --track samples/sample_track.json \
        --occlusion samples/sample_occlusion.json --output output/report.json
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from isometric_audit.tool import main

if __name__ == "__main__":
    sys.exit(main())
