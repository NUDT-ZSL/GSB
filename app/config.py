"""Shared configuration objects for site server, crawler and console.

All fault injection (503 / slow responses / session expiry / schema version
switching) is expressed as plain config so demos and tests are deterministic.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class SiteConfig:
    host: str = "127.0.0.1"
    port: int = 8701
    pages: int = 3
    per_page: int = 8
    # session_ttl > 0: an issued session token expires after this many
    # authenticated requests, answered with 401 SESSION_EXPIRED.
    session_ttl: int = 0
    # session_always_expired: every token is rejected (drives E_SESSION).
    session_always_expired: bool = False
    # rate_limit_inflight > 0: requests arriving while more than this many
    # requests are in flight get a 429 RATE_LIMITED response.
    rate_limit_inflight: int = 0
    # item_id -> number of 503 responses still to serve for that detail page.
    fail_503: dict = field(default_factory=dict)
    # item_id -> artificial response delay in milliseconds.
    slow_items: dict = field(default_factory=dict)
    # item_id -> schemaVersion override (version switching via config).
    version_overrides: dict = field(default_factory=dict)
    # items served with an unknown schemaVersion (unresolved path).
    unknown_items: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "SiteConfig":
        return cls(**json.loads(raw))


@dataclass
class SchedulerConfig:
    initial_inflight: int = 4
    min_inflight: int = 1
    max_inflight: int = 8          # hard cap, never exceeded
    increase_step: int = 1         # bounded additive increase
    increase_after: int = 5        # consecutive healthy observations per step
    decrease_factor: float = 0.5   # multiplicative decrease on pressure
    latency_high_ms: float = 400.0
    latency_window: int = 20
    retry_max: int = 3             # bounded retries per request
    backoff_base_ms: float = 50.0
    backoff_max_ms: float = 1000.0
    retry_queue_max: int = 16      # bounded number of waiting retry slots
    session_replay_max: int = 3    # bounded session re-acquire replays


@dataclass
class CrawlerConfig:
    site_url: str = "http://127.0.0.1:8701"
    db_path: str = "artifacts/uid8.db"
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    render_timeout_ms: float = 5000.0
    render_poll_ms: float = 50.0
    request_timeout_ms: float = 3000.0
    # crash_after_items > 0: raise SimulatedCrash after N confirmed items to
    # emulate a hard process kill for resume testing.
    crash_after_items: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "CrawlerConfig":
        data = json.loads(raw)
        data["scheduler"] = SchedulerConfig(**data.get("scheduler", {}))
        return cls(**data)


def load_json_file(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
