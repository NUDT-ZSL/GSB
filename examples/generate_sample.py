"""Generate the deterministic minute-level sample used by the demo and tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "examples" / "data" / "sli_minutes.ndjson"


def generate() -> Path:
    start = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DATA_PATH.open("w", encoding="utf-8", newline="\n") as handle:
        for index in range(240):
            minute = start + timedelta(minutes=index)
            requests = 100 + (index * 7) % 21
            errors = 0
            if 226 <= index <= 237:
                errors = min(20, requests)
            elif index in {75, 76}:
                errors = 1
            record = {
                "minute": minute.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "requests": requests,
                "errors": errors,
            }
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return DATA_PATH


if __name__ == "__main__":
    print(generate())

