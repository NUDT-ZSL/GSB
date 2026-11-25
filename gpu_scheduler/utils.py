from __future__ import annotations

import hashlib
import json
import os
import signal
from datetime import datetime, timezone
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_value(value: Any) -> str:
    return sha256_text(canonical_json(value))


def utc_wall_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def crash_hook(stage: str) -> None:
    """Kill the process when GPU_SCHEDULER_CRASH_AT requests this stage.

    ``SIGKILL`` is deliberate: neither Python exception handling nor SQLite's
    atexit handlers can commit extra state, making recovery tests meaningful.
    """

    requested = os.environ.get("GPU_SCHEDULER_CRASH_AT", "")
    if requested and requested == stage:
        os.kill(os.getpid(), signal.SIGKILL)
        # The next line is only a fallback for unusual signal handling.
        os._exit(137)
