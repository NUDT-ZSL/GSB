from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diagnostic_workbench.engine import Workbench


def main() -> int:
    state_dir = sys.argv[1]
    round_id = int(sys.argv[2])
    logical_time_ms = int(sys.argv[3])
    workbench = Workbench(state_dir)
    workbench.switch(
        {
            "logical_time_ms": logical_time_ms,
            "round_id": round_id,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

