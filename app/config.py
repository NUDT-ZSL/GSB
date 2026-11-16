from __future__ import annotations

import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = Path(os.getenv("AUDIT_DB_PATH", ROOT / "data" / "workbench.sqlite3"))
MAX_CONCURRENCY = int(os.getenv("AUDIT_MAX_CONCURRENCY", "8"))
DEFAULT_PORT = int(os.getenv("PORT", "8000"))
DEFAULT_HOST = os.getenv("HOST", "127.0.0.1")
DEFAULT_BASE_URL = os.getenv("PUBLIC_BASE_URL", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")

DEFAULT_TASK_CONFIG: dict[str, Any] = {
    "concurrency": 2,
    "delay_profile": "slow",
    "fault_profile": "mixed",
    "render_timeout_s": 4.0,
    "max_render_observations": 40,
    "max_retries": 3,
    "backoff_base_ms": 80,
    "backoff_max_ms": 800,
    "high_latency_ms": 500,
    "max_session_replays": 3,
    "request_timeout_s": 8.0,
}

TERMINAL_TASK_STATUSES = {"completed", "partial", "recoverable", "failed"}


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    config = {**DEFAULT_TASK_CONFIG, **(raw or {})}
    concurrency = int(config.get("concurrency", 2))
    if concurrency < 1 or concurrency > MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
    config["concurrency"] = concurrency
    for key in ("max_retries", "max_session_replays"):
        value = int(config[key])
        if value < 0 or value > 20:
            raise ValueError(f"{key} must be between 0 and 20")
        config[key] = value
    render_observations = int(config["max_render_observations"])
    if render_observations < 2 or render_observations > 200:
        raise ValueError("max_render_observations must be between 2 and 200")
    config["max_render_observations"] = render_observations
    for key in ("backoff_base_ms", "backoff_max_ms", "high_latency_ms"):
        config[key] = max(0, int(config[key]))
    if config["backoff_max_ms"] < config["backoff_base_ms"]:
        raise ValueError("backoff_max_ms must be >= backoff_base_ms")
    for key in ("render_timeout_s", "request_timeout_s"):
        config[key] = max(0.1, float(config[key]))
    allowed_delays = {"none", "slow", "bursty", "spike"}
    if config.get("delay_profile") not in allowed_delays:
        raise ValueError(f"delay_profile must be one of {sorted(allowed_delays)}")
    allowed_faults = {
        "none",
        "mixed",
        "session_expiry",
        "always_503",
        "render_timeout",
        "bad_cursor",
    }
    if config.get("fault_profile") not in allowed_faults:
        raise ValueError(f"fault_profile must be one of {sorted(allowed_faults)}")
    return config
