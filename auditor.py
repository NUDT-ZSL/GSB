"""项目根入口，便于以 `python -I auditor.py ...` 方式运行（隔离模式）。

-I 隐含 -P：当前工作目录不会自动进入 sys.path，因此先按本文件位置显式
注入项目根目录，再导入包。全程仅使用标准库，无网络访问。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cmd_guard.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
