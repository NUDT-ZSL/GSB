"""Windows / 跨平台命令行启动器。

支持以隔离模式运行：`python -I run_handover.py ...`
注意 Python 3.10 的 -I 不会把脚本目录加入 sys.path（3.11 起才加入），
因此在导入前显式把本文件所在目录（handover 包的父目录）放入 sys.path。
该目录是我们自己的代码目录，不受 -I 隔离目的（屏蔽外部 PYTHONPATH/当前目录）
影响。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from handover.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
