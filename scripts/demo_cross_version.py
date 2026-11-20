from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rope_attention.demo import run_demo


if __name__ == "__main__":
    result = run_demo(ROOT)
    print(
        json.dumps(
            {
                "business_codes": result["business_codes"],
                "incremental": result["incremental"],
                "migration": result["migration"],
                "ok": result["ok"],
                "schema": "rope-attention-cross-version-demo/v1",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )

