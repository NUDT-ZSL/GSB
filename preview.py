#!/usr/bin/env python
"""零安装启动器，兼容隔离模式（python -I preview.py ...）。

显式把仓库根目录加入 sys.path，使包内绝对导入在 -I（cwd 不进路径）
与普通模式下都能工作；脚本本身不包含业务逻辑。
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from material_preview.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
