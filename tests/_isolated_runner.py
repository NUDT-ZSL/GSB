"""测试专用隔离启动器。

以 ``python -I tests/_isolated_runner.py ...`` 运行：
``-I`` 保证解释器处于隔离模式（忽略环境变量/用户 site 包/当前目录脚本目录
之外的路径注入），而仓库根目录由本文件显式加入 ``sys.path``，
从而在隔离模式下仍能导入仓库内的 wav_splitter 包。
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wav_splitter.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
