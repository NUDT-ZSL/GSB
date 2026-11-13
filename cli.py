"""命令行入口脚本，支持隔离模式直接运行：

    python -I cli.py --input ... --clips ... --output-dir ...

（-I 隔离模式下 -m 无法解析工作目录中的包，故提供本文件。）
"""

import os
import sys

# Windows 控制台默认 GBK，统一切到 UTF-8，保证中文报告信息不乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# 隔离模式下显式把项目根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_cutter.cutter import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
