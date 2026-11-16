from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from .config import E_HTTP, E_RETRY_EXHAUSTED, E_SESSION, E_TIMEOUT, TaskConfig


class Stats:
    def __init__(self) -> None:
        self.http_attempts = 0
        self.retries = 0
        self.session_replays = 0
        self.render_observations = 0

    def report(self) -> dict:
        return {
            "http_attempts": self.http_attempts,
            "retries": self.retries,
            "session_replays": self.session_replays,
            "render_observations": self.render_observations,
        }


class Budgets:
    """Hard budgets for in-flight requests and the retry-wait queue.
    Callers block on the semaphore (i.e. queue) when a budget is exhausted
    instead of opening unbounded connections."""

    def __init__(self, in_flight_limit: int, retry_wait_limit: int):
        self.in_flight_limit = in_flight_limit
        self.retry_wait_limit = retry_wait_limit
        self._in_flight_sem = asyncio.Semaphore(in_flight_limit)
        self._retry_wait_sem = asyncio.Semaphore(retry_wait_limit)
        self.in_flight_current = 0
        self.in_flight_peak = 0
        self.in_flight_leases = 0
        self.retry_wait_current = 0
        self.retry_wait_peak = 0
        self.retry_wait_leases = 0

    @asynccontextmanager
    async def in_flight(self):
        await self._in_flight_sem.acquire()
        self.in_flight_current += 1
        self.in_flight_leases += 1
        self.in_flight_peak = max(self.in_flight_peak, self.in_flight_current)
        try:
            yield
        finally:
            self.in_flight_current -= 1
            self._in_flight_sem.release()

    @asynccontextmanager
    async def retry_wait(self):
        await self._retry_wait_sem.acquire()
        self.retry_wait_current += 1
        self.retry_wait_leases += 1
        self.retry_wait_peak = max(self.retry_wait_peak, self.retry_wait_current)
        try:
            yield
        finally:
            self.retry_wait_current -= 1
            self._retry_wait_sem.release()

    def report(self) -> dict:
        return {
            "concurrency_limit": self.in_flight_limit,
            "in_flight_peak": self.in_flight_peak,
            "in_flight_leases": self.in_flight_leases,
            "in_flight_within_budget": self.in_flight_peak <= self.in_flight_limit,
            "retry_wait_limit": self.retry_wait_limit,
            "retry_wait_peak": self.retry_wait_peak,
            "retry_wait_leases": self.retry_wait_leases,
            "retry_wait_within_budget": self.retry_wait_peak <= self.retry_wait_limit,
        }


class SessionManager:
    def __init__(self, client: httpx.AsyncClient, budgets: Budgets, stats: Stats):
        self._client = client
        self._budgets = budgets
        self._stats = stats
        self._token: str | None = None
        self._gen = 0
        self._lock = asyncio.Lock()

    async def ensure(self) -> None:
        if self._token is None:
            await self.refresh()

    async def refresh(self, stale_gen: int | None = None) -> None:
        async with self._lock:
            if stale_gen is not None and self._gen != stale_gen:
                return
            async with self._budgets.in_flight():
                self._stats.http_attempts += 1
                resp = await self._client.get("/session")
                resp.raise_for_status()
            token = resp.json()["token"]
            self._client.cookies.set("sid", token)
            self._token = token
            self._gen += 1

    @property
    def generation(self) -> int:
        return self._gen


@dataclass
class FetchOutcome:
    ok: bool
    status_code: int | None = None
    text: str = ""
    error_class: str | None = None


def _is_session_expired(resp: httpx.Response) -> bool:
    return resp.status_code == 401 and resp.headers.get("X-Session-Expired") == "1"


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, budgets: Budgets,
                 session: SessionManager, cfg: TaskConfig, stats: Stats):
        self._client = client
        self._budgets = budgets
        self._session = session
        self._cfg = cfg
        self._stats = stats

    async def get(self, url: str) -> FetchOutcome:
        await self._session.ensure()
        retries = 0
        replays = 0
        while True:
            gen = self._session.generation
            resp: httpx.Response | None
            async with self._budgets.in_flight():
                self._stats.http_attempts += 1
                try:
                    resp = await asyncio.wait_for(
                        self._client.get(url, timeout=self._cfg.request_timeout),
                        timeout=self._cfg.request_timeout + 0.05,
                    )
                except (httpx.TimeoutException, asyncio.TimeoutError):
                    resp = None
            if resp is not None and _is_session_expired(resp):
                if replays >= self._cfg.session_replay_limit:
                    return FetchOutcome(False, resp.status_code, "", E_SESSION)
                await self._session.refresh(stale_gen=gen)
                replays += 1
                self._stats.session_replays += 1
                continue
            if resp is not None and resp.status_code == 200:
                return FetchOutcome(True, resp.status_code, resp.text)
            if resp is not None and resp.status_code < 500:
                return FetchOutcome(False, resp.status_code, resp.text, E_HTTP)
            if retries >= self._cfg.max_retries:
                error = E_TIMEOUT if resp is None else E_RETRY_EXHAUSTED
                return FetchOutcome(False, None if resp is None else resp.status_code, "", error)
            async with self._budgets.retry_wait():
                self._stats.retries += 1
                delay = min(self._cfg.backoff_cap, self._cfg.backoff_base * (2 ** retries))
                await asyncio.sleep(delay)
            retries += 1
