#!/usr/bin/env python3
"""固定种子生成本地确定性样本。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multiscreen.generator import DEFAULT_SEED, generate_sample  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成多屏状态机固定种子样本")
    parser.add_argument("--out", default=str(ROOT / "task"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    result = generate_sample(args.out, args.seed)
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
