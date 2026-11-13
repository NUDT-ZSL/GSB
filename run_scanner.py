"""项目根目录启动器：支持在隔离模式（python -I）下直接运行。

-isolated 模式（3.11+ 的 -I 隐含 -P）下脚本目录不会自动进入 sys.path，
这里显式加入“启动器自身所在目录”（受信任的项目目录，而非当前工作目录）。

用法示例：
  python -I run_scanner.py scan <目录> -o report.json
  python -I run_scanner.py compare <修复前目录> <修复后目录> -o diff.json
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from pysentry.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
