from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rope_attention.process_demo import run_process_pool_demo


if __name__ == "__main__":
    print(json.dumps(run_process_pool_demo(ROOT), ensure_ascii=False, indent=2, sort_keys=True))

