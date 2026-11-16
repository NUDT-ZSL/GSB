"""异步采集器：延迟渲染就绪判定、并发抓取、退避重试、会话重放、断点续跑。"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .parsers import parse_detail
from .store import Store

MAX_CONCURRENCY = 8


class CollectorError(Exception):
    code = "E_INTERNAL"


class SessionExhausted(CollectorError):
    code = "E_SESSION"


class RenderTimeout(CollectorError):
    code = "E_RENDER_TIMEOUT"

    def __init__(self, message: str, snapshot: str):
        super().__init__(message)
        self.snapshot = snapshot


class ItemRetryExhausted(CollectorError):
    code = "E_HTTP_503"


class PauseRequested(Exception):
    pass


@dataclass
class CollectorConfig:
    base_url: str
    page_size: int = 5
    concurrency: int = 4
    max_retries: int = 4
    backoff_base_ms: int = 100
    backoff_max_ms: int = 2000
    render_timeout_ms: int = 5000
    render_poll_ms: int = 100
    render_stable_polls: int = 3
    session_replay_limit: int = 8
    source: str = "run"

    @classmethod
    def from_dict(cls, d: dict) -> "CollectorConfig":
        cfg = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        cfg.concurrency = max(1, min(cfg.concurrency, MAX_CONCURRENCY))
        return cfg

    def backoff(self, attempt: int) -> float:
        return min(self.backoff_base_ms * (2 ** (attempt - 1)), self.backoff_max_ms) / 1000.0


class SessionManager:
    """会话获取与有上限的重放。"""

    def __init__(self, client: httpx.AsyncClient, replay_limit: int):
        self.client = client
        self.limit = replay_limit
        self.replays = 0

    async def relogin(self) -> None:
        self.replays += 1
        if self.replays > self.limit:
            raise SessionExhausted(f"session replay limit {self.limit} exceeded")
        r = await self.client.get("/session")
        r.raise_for_status()

    @property
    def sid(self) -> str | None:
        return self.client.cookies.get("sid")


async def _sync_browser_cookies(context, client: httpx.AsyncClient, base_url: str) -> None:
    sid = client.cookies.get("sid")
    if sid:
        await context.add_cookies([{"name": "sid", "value": sid, "url": base_url}])


async def wait_render_ready(page, cfg: CollectorConfig) -> tuple[str, list[int]]:
    """显式就绪判定：容器标记出现 + 条目计数稳定。返回 (状态, 观测计数序列)。"""
    sel = '#ticket-list[data-render-done="true"], #ticket-list[data-render-done="error"]'
    await page.wait_for_selector(sel, timeout=cfg.render_timeout_ms, state="attached")
    state = await page.locator("#ticket-list").get_attribute("data-render-done")
    counts: list[int] = []
    if state != "true":
        return state, counts
    last, stable = -1, 0
    deadline = time.monotonic() + cfg.render_timeout_ms / 1000.0
    while stable < cfg.render_stable_polls:
        if time.monotonic() > deadline:
            raise PlaywrightTimeoutError("render count not stable before deadline")
        count = await page.locator("#ticket-list .ticket-item").count()
        counts.append(count)
        stable = stable + 1 if count == last else 0
        last = count
        if stable < cfg.render_stable_polls:
            await page.wait_for_timeout(cfg.render_poll_ms)  # 轮询间隔，非就绪 sleep
    return "ok", counts


async def _fetch_list(page, context, client, session, cfg, page_num) -> tuple[list[dict], dict]:
    last_err = "unknown"
    counts: list[int] = []
    for attempt in range(1, cfg.max_retries + 2):
        await _sync_browser_cookies(context, client, cfg.base_url)
        await page.goto(f"{cfg.base_url}/tickets?page={page_num}&size={cfg.page_size}",
                        wait_until="domcontentloaded")
        try:
            status, counts = await wait_render_ready(page, cfg)
        except PlaywrightTimeoutError:
            last_err = f"render timeout on attempt {attempt}"
        else:
            if status == "ok":
                items = []
                for el in await page.locator("#ticket-list .ticket-item").all():
                    items.append({
                        "ticket_id": await el.get_attribute("data-ticket-id"),
                        "version": await el.get_attribute("data-version"),
                    })
                meta = json.loads(await page.locator("#page-meta").inner_text())
                return items, meta
            last_err = f"list render reported '{status}' on attempt {attempt}"
            await session.relogin()  # 可能是会话过期导致页面内 fetch 401
        await asyncio.sleep(cfg.backoff(attempt))
    snapshot = json.dumps({
        "page": page_num,
        "url": page.url,
        "observed_counts": counts,
        "html_excerpt": (await page.content())[:4000],
    }, ensure_ascii=False)
    raise RenderTimeout(f"list page {page_num} not ready: {last_err}", snapshot)


async def _fetch_detail(client, session, cfg, ticket_id) -> str:
    attempt = 0
    while True:
        r = await client.get(f"/tickets/{ticket_id}")
        if r.status_code == 503:
            attempt += 1
            if attempt > cfg.max_retries:
                raise ItemRetryExhausted(
                    f"503 for {ticket_id} after {cfg.max_retries} retries")
            await asyncio.sleep(cfg.backoff(attempt))
            continue
        if r.status_code == 401:
            code = None
            try:
                code = r.json().get("code")
            except Exception:
                pass
            if code == "E_SESSION":
                await session.relogin()  # 重新获取会话后重放当前请求
                continue
        r.raise_for_status()
        return r.text


async def collect_task(store: Store, task_id: int, pause_check=None) -> None:
    task = store.get_task(task_id)
    cfg = CollectorConfig.from_dict(task["config"])
    pause_check = pause_check or (lambda: False)
    source = f"task-{task_id}-{cfg.source}-{int(time.time())}"

    done = store.load_checkpoints(task_id)
    page_num = store.get_cursor(task_id)

    async with httpx.AsyncClient(base_url=cfg.base_url, timeout=15.0) as client:
        session = SessionManager(client, cfg.session_replay_limit)
        await session.relogin()
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()
                while True:
                    if pause_check():
                        raise PauseRequested()
                    items, meta = await _fetch_list(page, context, client, session, cfg, page_num)
                    todo = [it for it in items if it["ticket_id"] not in done]
                    sem = asyncio.Semaphore(cfg.concurrency)

                    async def one(it):
                        async with sem:
                            if pause_check():
                                raise PauseRequested()
                            tid = it["ticket_id"]
                            try:
                                html = await _fetch_detail(client, session, cfg, tid)
                            except ItemRetryExhausted as e:
                                store.record_item_failure(task_id, tid, e.code, str(e))
                                return
                            parsed = parse_detail(html, tid)
                            store.upsert_item(task_id, tid, parsed.version,
                                              parsed.parse_status, parsed.raw,
                                              parsed.normalized, source)
                            store.add_checkpoint(task_id, tid, parsed.version)

                    results = await asyncio.gather(
                        *(one(it) for it in todo), return_exceptions=True)
                    for r in results:
                        if isinstance(r, PauseRequested):
                            raise r
                        if isinstance(r, Exception):
                            raise r
                    if page_num >= meta["pages"]:
                        break
                    page_num += 1
                    store.set_cursor(task_id, page_num)
            finally:
                await browser.close()


async def run_task(store: Store, task_id: int, pause_check=None) -> None:
    """执行采集任务并负责终态迁移（供控制台 worker 与测试复用）。"""
    try:
        await collect_task(store, task_id, pause_check)
    except SessionExhausted as e:
        store.set_error(task_id, e.code, str(e))
        store.transition(task_id, "paused", note="session replay exhausted")
    except RenderTimeout as e:
        store.save_snapshot(task_id, "render_timeout", e.snapshot)
        store.set_error(task_id, e.code, str(e))
        store.transition(task_id, "failed", note="render timeout")
    except PauseRequested:
        store.transition(task_id, "paused", note="pause requested")
    except Exception as e:  # noqa: BLE001
        store.set_error(task_id, "E_INTERNAL", repr(e))
        store.transition(task_id, "failed", note="internal error")
    else:
        store.set_error(task_id, None, None)
        store.transition(task_id, "succeeded", note="completed")
