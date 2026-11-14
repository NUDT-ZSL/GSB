"""生成确定性样本到 samples/ 目录。

运行（仓库根目录）:
    "C:\\Users\\27857\\AppData\\Roaming\\uv\\python\\cpython-3.11.16-windows-x86_64-none\\python.exe" generate_samples.py
或任意 Python 3.11+:
    python generate_samples.py
"""

import os
import sys

# 兼容 python -I（隔离模式不自动把脚本目录加入 sys.path）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from isometric_audit.generate_sample import generate

if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
    paths = generate(out_dir)
    for key, path in sorted(paths.items()):
        if key == "notes":
            continue
        print(f"{key}: {os.path.relpath(path)}")
