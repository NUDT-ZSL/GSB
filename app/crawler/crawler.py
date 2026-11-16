"""Crawler: list readiness detection, pagination, adaptive fetching, session
replay, versioned storage and resumable checkpoints.

Failure model:
  * ItemFailure  -> item-level failure (rate_limited / transient / parse),
                    recorded and counted, task continues;
  * TaskPaused   -> task-level pause (E_RENDER_TIMEOUT / E_SESSION), the task
                    stays resumable from its checkpoint;
  * SimulatedCrash -> emulates a hard kill; only the checkpoint in SQLite may
                    be used to resume.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from ..config import CrawlerConfig
from ..storage.db import Store, initial_stats
from .parser import ParseFailure, parse_detail, parse_list_meta
from .scheduler import AdaptiveScheduler

E_RENDER_TIMEOUT = "E_RENDER_TIMEOUT"
E_SESSION = "E_SESSION"
E_PARSE = "E_PARSE"
E_RETRY_EXHAUSTED = "E_RETRY_EXHAUSTED"


class ItemFailure(Exception):
    def __init__(self, category: str, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.category = category
        self.code = code
        self.detail = detail


class TaskPaused(Exception):
    def __init__(self, code: str, category: str, item_key: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.category = category
        self.item_key = item_key
        self.detail = detail


class SimulatedCrash(Exception):
    pass


class Crawler:
    def __init__(self, store: Store, cfg: CrawlerConfig, task_id: str):
        self.store = store
        self.cfg = cfg
        self.task_id = task_id
        self.scheduler = AdaptiveScheduler(cfg.scheduler)
        self.stats = initial_stats()
        self.completed: set = set()
        self.session_token: str | None = None

    # ---------------- checkpoint helpers ----------------

    def _save(self, status: str | None = None, cursor_page=None):
        self.store.save_checkpoint(
            self.task_id,
            status=status,
            cursor_page=cursor_page,
            completed_keys=sorted(self.completed),
            scheduler_state=self.scheduler.snapshot(),
            stats=self.stats,
        )

    def _load(self) -> int | None:
        task = self.store.get_task(self.task_id)
        if task is None:
            self.store.create_task(self.task_id)
            return 1
        for key in initial_stats():
            self.stats.setdefault(key, task["stats"].get(key, initial_stats()[key]))
        self.stats.update({k: v for k, v in task["stats"].items() if k in self.stats})
        self.completed = set(task["completed_keys"])
        self.scheduler.restore(task["scheduler_state"])
        self.store.save_checkpoint(self.task_id, status="running")
        return task["cursor_page"]

    # ---------------- HTTP with retry / session replay ----------------

    async def _new_session(self, client: httpx.AsyncClient) -> str:
        async with self.scheduler.slot():
            resp = await client.post("/session")
        resp.raise_for_status()
        return resp.json()["token"]

    async def _request(self, client: httpx.AsyncClient, method: str, path: str) -> httpx.Response:
        cfg = self.cfg.scheduler
        attempt = 0
        replays = 0
        while True:
            headers = {"X-Session-Token": self.session_token} if self.session_token else {}
            resp = None
            latency = -1.0
            try:
                async with self.scheduler.slot():
                    start = time.monotonic()
                    resp = await client.request(method, path, headers=headers)
                    latency = (time.monotonic() - start) * 1000.0
            except httpx.TimeoutException:
                self.scheduler.observe("timeout", -1)
                attempt += 1
                if attempt > cfg.retry_max:
                    raise ItemFailure("transient", E_RETRY_EXHAUSTED,
                                      f"timeout after {attempt} attempts on {path}")
                self.stats["retries"] += 1
                await self.scheduler.retry_wait(attempt)
                continue

            if resp.status_code == 401:
                # session expired: re-acquire and replay within the same budget
                replays += 1
                if replays > cfg.session_replay_max:
                    raise TaskPaused(E_SESSION, "session", path,
                                     f"session replay limit {cfg.session_replay_max} exceeded")
                self.stats["session_replays"] += 1
                self.session_token = await self._new_session(client)
                continue
            if resp.status_code == 429:
                self.scheduler.observe("rate_limited", latency)
                attempt += 1
                if attempt > cfg.retry_max:
                    raise ItemFailure("rate_limited", E_RETRY_EXHAUSTED,
                                      f"429 after {attempt} attempts on {path}")
                self.stats["retries"] += 1
                await self.scheduler.retry_wait(attempt)
                continue
            if resp.status_code >= 500:
                self.scheduler.observe("server_error", latency)
                attempt += 1
                if attempt > cfg.retry_max:
                    raise ItemFailure("transient", E_RETRY_EXHAUSTED,
                                      f"{resp.status_code} after {attempt} attempts on {path}")
                self.stats["retries"] += 1
                await self.scheduler.retry_wait(attempt)
                continue
            if resp.status_code != 200:
                raise ItemFailure("transient", "E_HTTP",
                                  f"unexpected status {resp.status_code} on {path}")
            self.scheduler.observe("success", latency)
            self.store.record_raw(self.task_id, path, 200, resp.text)
            return resp

    # ---------------- list readiness & pagination ----------------

    async def _await_render(self, client: httpx.AsyncClient, meta: dict, page: int) -> list:
        """Ready = item container declared AND item count stable across two
        consecutive observations. Never a fixed wait."""
        observations = []
        previous = None
        deadline = time.monotonic() + self.cfg.render_timeout_ms / 1000.0
        while time.monotonic() < deadline:
            resp = await self._request(client, "GET", meta["render_src"])
            data = resp.json()
            count = len(data.get("items", []))
            observations.append(count)
            if previous is not None and count == previous and (count > 0 or len(observations) >= 3):
                return data["items"]
            previous = count
            await asyncio.sleep(self.cfg.render_poll_ms / 1000.0)
        snapshot = {"page": page, "render_src": meta["render_src"], "observations": observations}
        raise TaskPaused(E_RENDER_TIMEOUT, "render", f"page:{page}", json.dumps(snapshot))

    # ---------------- item processing ----------------

    async def _process_item(self, client: httpx.AsyncClient, item: dict) -> None:
        key = item["item_id"]
        try:
            resp = await self._request(client, "GET", item["href"])
        except ItemFailure as failure:
            self.store.add_failure(self.task_id, key, failure.category,
                                   failure.code, failure.detail)
            self.stats["failures"][failure.category] = \
                self.stats["failures"].get(failure.category, 0) + 1
            self.completed.add(key)
            self._save()
            return
        try:
            result = parse_detail(resp.text)
        except ParseFailure as failure:
            self.store.add_failure(self.task_id, key, "parse", E_PARSE, str(failure))
            self.stats["failures"]["parse"] = self.stats["failures"].get("parse", 0) + 1
            self.completed.add(key)
            self._save()
            return
        outcome = self.store.store_crawled(
            self.task_id, key, result.schema_version, result.normalized, result.evidence)
        if outcome == "inserted":
            self.stats["entries_inserted"] += 1
        elif outcome == "identical":
            self.stats["entries_identical"] += 1
        elif outcome == "updated":
            self.stats["entries_updated"] += 1
        elif outcome == "conflict":
            self.stats["conflicts"] += 1
        if result.unresolved:
            self.stats["unresolved"] += 1
        self.completed.add(key)
        self._save()

    # ---------------- main loop ----------------

    async def run(self) -> dict:
        cursor = self._load()
        timeout = httpx.Timeout(self.cfg.request_timeout_ms / 1000.0)
        async with httpx.AsyncClient(base_url=self.cfg.site_url, timeout=timeout) as client:
            self.session_token = await self._new_session(client)
            try:
                while cursor is not None:
                    resp = await self._request(client, "GET", f"/list?page={cursor}")
                    meta = parse_list_meta(resp.text)
                    items = await self._await_render(client, meta, cursor)
                    await asyncio.gather(*(
                        self._process_item(client, item) for item in items
                        if item["item_id"] not in self.completed
                    ))
                    next_page = meta["next_page"]
                    if next_page is not None and next_page > meta["total_pages"]:
                        next_page = None
                    cursor = next_page
                    self._save(cursor_page=cursor)
                    if self.cfg.crash_after_items and \
                            len(self.completed) >= self.cfg.crash_after_items:
                        raise SimulatedCrash(
                            f"killed after {len(self.completed)} confirmed items")
                self._save(status="done", cursor_page=None)
            except SimulatedCrash:
                self._save(status="crashed")
                raise
            except TaskPaused as paused:
                self.store.add_failure(self.task_id, paused.item_key, paused.category,
                                       paused.code, paused.detail)
                self.stats["failures"][paused.category] = \
                    self.stats["failures"].get(paused.category, 0) + 1
                self._save(status="paused")
        return self.summary()

    def summary(self) -> dict:
        task = self.store.get_task(self.task_id)
        return {
            "task_id": self.task_id,
            "status": task["status"],
            "completed": len(task["completed_keys"]),
            "stats": task["stats"],
            "scheduler": task["scheduler_state"],
        }
