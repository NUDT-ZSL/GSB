"""命令行脚本：确定性重建样本清单与帧 PNG。

用法：python tools/generate_sample.py [目标目录]（默认 sample/）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from animstrip.samplegen import generate  # noqa: E402


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample")
    manifest = generate(target)
    print(f"样本已生成：{manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
