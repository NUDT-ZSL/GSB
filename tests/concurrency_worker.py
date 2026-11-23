from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diagnostic_workbench.engine import Workbench
from diagnostic_workbench.util import canonical_json_text


def main() -> int:
    state_dir = sys.argv[1]
    round_id = int(sys.argv[2])
    logical_time_ms = int(sys.argv[3])
    workbench = Workbench(state_dir)
    result = workbench.switch(
        {
            "logical_time_ms": logical_time_ms,
            "round_id": round_id,
        }
    )
    sys.stdout.write(canonical_json_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

