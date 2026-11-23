from __future__ import annotations

import os
import time
from pathlib import Path


def maybe_fault(point: str) -> None:
    """Test-only crash hook enabled through environment variables.

    The hook intentionally does not make fault behavior part of normal gateway
    logic. It is used by the independent recovery demonstration process.
    """

    if os.environ.get("GATEWAY_FAULT_POINT") != point:
        return
    marker = os.environ.get("GATEWAY_FAULT_MARKER")
    if marker:
        path = Path(marker)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(point, encoding="utf-8")
    delay = float(os.environ.get("GATEWAY_FAULT_DELAY_SECONDS", "60"))
    time.sleep(max(0.0, delay))

