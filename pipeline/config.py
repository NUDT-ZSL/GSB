from __future__ import annotations

from dataclasses import dataclass, field

E_RENDER_TIMEOUT = "E_RENDER_TIMEOUT"
E_DUPLICATE = "E_DUPLICATE"
E_TIMEOUT = "E_TIMEOUT"
E_RETRY_EXHAUSTED = "E_RETRY_EXHAUSTED"
E_SESSION = "E_SESSION"
E_HTTP = "E_HTTP"

MAX_CONCURRENCY_HARD_LIMIT = 16
MAX_RETRY_WAIT_HARD_LIMIT = 32


@dataclass
class TaskConfig:
    task_id: str
    sources: list[str]
    concurrency: int = 4
    retry_wait_budget: int = 8
    max_retries: int = 2
    session_replay_limit: int = 3
    request_timeout: float = 0.3
    backoff_base: float = 0.05
    backoff_cap: float = 0.4
    render_max_observations: int = 8
    render_interval: float = 0.05
    biz_filter: list[str] | None = None

    def __post_init__(self) -> None:
        self.concurrency = max(1, min(int(self.concurrency), MAX_CONCURRENCY_HARD_LIMIT))
        self.retry_wait_budget = max(1, min(int(self.retry_wait_budget), MAX_RETRY_WAIT_HARD_LIMIT))

    @classmethod
    def from_dict(cls, data: dict) -> "TaskConfig":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}
