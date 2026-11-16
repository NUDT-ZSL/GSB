from __future__ import annotations

import asyncio
import hashlib
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx

from app.db import Database, canonical_json
from app.parsers import DetailParseError, parse_detail
from app.render import (
    ListContract,
    RenderContractError,
    RenderObservation,
    parse_fragment,
    parse_list_contract,
    validate_cursor,
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class CrawlError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class ItemError(CrawlError):
    pass


class RetryableError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class FetchResult:
    url: str
    status_code: int
    text: str
    headers: dict[str, str]
    duration_ms: int
    request_seq: int | None
    attempts: int


class SessionManager:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        db: Database,
        task_id: str,
        wave: str,
        config: dict[str, Any],
        base_url: str,
        in_flight: asyncio.Semaphore,
    ):
        self.client = client
        self.db = db
        self.task_id = task_id
        self.wave = wave
        self.config = config
        self.base_url = base_url.rstrip("/")
        self.in_flight = in_flight
        self.lock = asyncio.Lock()
        self.token: str | None = None
        self.archive_session_id: str | None = None
        self.crawl_session_id: str | None = None

    @property
    def session_query(self) -> dict[str, str]:
        return {
            "wave": self.wave,
            "delay_profile": str(self.config.get("delay_profile", "none")),
            "fault_profile": str(self.config.get("fault_profile", "none")),
        }

    async def ensure(self) -> str:
        async with self.lock:
            if self.token:
                return self.token
            return await self._acquire_locked()

    async def renew(self, seen_token: str | None) -> str:
        async with self.lock:
            if self.token and self.token != seen_token:
                return self.token
            if self.crawl_session_id:
                self.db.increment_session_replays(self.crawl_session_id)
            return await self._acquire_locked()

    async def _acquire_locked(self) -> str:
        url = f"{self.base_url}/archive/session"
        params = self.session_query
        async with self.in_flight:
            response = await self.client.get(url, params=params)
        if response.status_code != 200:
            raise RetryableError("E_SESSION_ACQUIRE", f"session acquisition failed: {response.status_code}")
        payload = response.json()
        token = payload.get("session_id")
        if not token:
            raise RetryableError("E_SESSION_ACQUIRE", "session response has no session_id")
        crawl_session_id = f"cs_{uuid.uuid4().hex[:16]}"
        self.db.create_crawl_session(crawl_session_id, self.task_id, str(token), self.wave)
        self.crawl_session_id = crawl_session_id
        self.archive_session_id = str(token)
        self.token = str(token)
        return self.token


class ArchiveClient:
    def __init__(
        self,
        *,
        db: Database,
        task_id: str,
        wave: str,
        config: dict[str, Any],
        base_url: str,
    ):
        self.db = db
        self.task_id = task_id
        self.wave = wave
        self.config = config
        self.base_url = base_url.rstrip("/")
        self.in_flight = asyncio.Semaphore(int(config["concurrency"]))
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(float(config["request_timeout_s"])),
            follow_redirects=True,
            headers={"User-Agent": "audit-incremental-workbench/1.0"},
        )
        self.sessions = SessionManager(
            client=self.http,
            db=db,
            task_id=task_id,
            wave=wave,
            config=config,
            base_url=self.base_url,
            in_flight=self.in_flight,
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def ensure_session(self) -> str:
        return await self.sessions.ensure()

    async def fetch(
        self,
        url: str,
        *,
        kind: str,
        item_key: str | None = None,
        attempts_offset: int = 0,
    ) -> FetchResult:
        max_retries = int(self.config["max_retries"])
        base_backoff = int(self.config["backoff_base_ms"]) / 1000.0
        max_backoff = int(self.config["backoff_max_ms"]) / 1000.0
        last_code = "E_RETRY_EXHAUSTED"
        for attempt in range(max_retries + 1):
            try:
                result = await self._fetch_with_session(url, kind=kind, item_key=item_key, attempt=attempt + 1)
                result.attempts = attempts_offset + attempt + 1
                if item_key:
                    self.db.update_item_attempt(self.task_id, item_key, result.attempts)
                return result
            except ItemError as exc:
                exc.details.setdefault("attempts", attempts_offset + attempt + 1)
                raise
            except RetryableError as exc:
                last_code = exc.code
                attempts = attempts_offset + attempt + 1
                if item_key:
                    self.db.update_item_attempt(self.task_id, item_key, attempts, exc.code)
                if attempt >= max_retries:
                    raise ItemError(
                        "E_RETRY_EXHAUSTED",
                        f"retry budget exhausted after {attempts} attempts; last={exc.code}",
                        {"last_code": exc.code, "attempts": attempts},
                    ) from exc
                delay = min(max_backoff, base_backoff * (2**attempt))
                if delay > 0:
                    await asyncio.sleep(delay)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_code = "E_NETWORK"
                attempts = attempts_offset + attempt + 1
                if item_key:
                    self.db.update_item_attempt(self.task_id, item_key, attempts, "E_NETWORK")
                if attempt >= max_retries:
                    raise ItemError(
                        "E_RETRY_EXHAUSTED",
                        f"network retry budget exhausted after {attempts} attempts",
                        {"last_code": "E_NETWORK", "attempts": attempts},
                    ) from exc
                delay = min(max_backoff, base_backoff * (2**attempt))
                if delay > 0:
                    await asyncio.sleep(delay)
        raise ItemError("E_RETRY_EXHAUSTED", f"retry budget exhausted; last={last_code}")

    async def _fetch_with_session(
        self, url: str, *, kind: str, item_key: str | None, attempt: int
    ) -> FetchResult:
        await self.sessions.ensure()
        max_replays = int(self.config["max_session_replays"])
        for replay in range(max_replays + 1):
            seen_token = self.http.cookies.get("archive_session") or self.sessions.token
            started = time.perf_counter()
            try:
                async with self.in_flight:
                    response = await self.http.get(url, headers={"Accept": "text/html,application/json"})
            finally:
                duration_ms = int((time.perf_counter() - started) * 1000)
            text = response.text
            headers = {str(key): str(value) for key, value in response.headers.items()}
            seq_raw = headers.get("x-archive-seq") or headers.get("X-Archive-Seq")
            request_seq = int(seq_raw) if seq_raw and seq_raw.isdigit() else None
            error_code = None
            body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if response.status_code == 401 or "E_SESSION_EXPIRED" in text[:1000]:
                error_code = "E_SESSION_EXPIRED"
            elif response.status_code == 503:
                error_code = "E_UPSTREAM_503"
            elif response.status_code >= 400:
                error_code = f"E_HTTP_{response.status_code}"
            elif duration_ms > int(self.config["high_latency_ms"]):
                error_code = "E_HIGH_LATENCY"
            self.db.record_request_event(
                {
                    "task_id": self.task_id,
                    "session_id": self.sessions.crawl_session_id,
                    "item_key": item_key,
                    "url": url,
                    "kind": kind,
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                    "error_code": error_code,
                    "fetched_at": utcnow(),
                    "body_sha256": body_hash,
                    "request_seq": request_seq,
                }
            )
            if error_code == "E_SESSION_EXPIRED":
                # The expired body is intentionally retained only as a hash in the
                # request event; it is never eligible for raw/normalized ingestion.
                if replay >= max_replays:
                    raise ItemError(
                        "E_SESSION",
                        f"session replay budget exhausted after {max_replays} replays",
                        {"replays": max_replays},
                    )
                await self.sessions.renew(seen_token)
                continue
            if error_code == "E_UPSTREAM_503":
                raise RetryableError("E_UPSTREAM_503", "archive returned 503")
            if error_code and error_code.startswith("E_HTTP_"):
                raise ItemError(error_code, f"archive returned HTTP {response.status_code}")
            if error_code == "E_HIGH_LATENCY":
                raise RetryableError(
                    "E_HIGH_LATENCY",
                    f"response exceeded high-latency threshold: {duration_ms}ms",
                )
            return FetchResult(
                url=str(response.url),
                status_code=response.status_code,
                text=text,
                headers=headers,
                duration_ms=duration_ms,
                request_seq=request_seq,
                attempts=attempt,
            )
        raise ItemError("E_SESSION", f"session replay budget exhausted after {max_replays} replays")


class Crawler:
    def __init__(
        self,
        *,
        db: Database,
        task: dict[str, Any],
        archive_base_url: str,
    ):
        self.db = db
        self.task = task
        self.task_id = task["id"]
        self.wave = task["wave"]
        self.config = task["config"]
        self.archive_base_url = archive_base_url.rstrip("/")
        self.client = ArchiveClient(
            db=db,
            task_id=self.task_id,
            wave=self.wave,
            config=self.config,
            base_url=self.archive_base_url,
        )

    async def run(self) -> None:
        try:
            checkpoint = self.db.get_checkpoint(self.task_id)
            cursor = checkpoint["cursor"]
            if not cursor.get("enumeration_complete"):
                await self._enumerate_pages(cursor)
            pending = self.db.pending_items(self.task_id)
            if pending:
                await self._process_items(pending)
            failures = self.db.task_failures(self.task_id)
            status = "partial" if failures else "completed"
            task_code = failures[0]["business_code"] if failures else None
            self.db.complete_task_sessions(self.task_id)
            summary = self.db.task_summary(self.task_id)
            summary["task_status"] = status
            summary["task_code"] = task_code
            summary["recoverable"] = status == "partial"
            summary["failure_codes"] = sorted(
                {item["business_code"] for item in failures if item["business_code"]}
            )
            self.db.finish_task(self.task_id, status, task_code, summary)
        except (RenderContractError, CrawlError) as exc:
            self.db.mark_interrupted_sessions(self.task_id)
            summary = self.db.task_summary(self.task_id)
            summary["task_status"] = "recoverable"
            summary["task_code"] = exc.code
            summary["recoverable"] = True
            summary["error_details"] = exc.details
            self.db.finish_task(
                self.task_id,
                "recoverable",
                exc.code,
                summary,
                error=str(exc),
            )
        except Exception as exc:
            self.db.mark_interrupted_sessions(self.task_id)
            summary = self.db.task_summary(self.task_id)
            summary["task_status"] = "failed"
            summary["task_code"] = "E_TASK"
            summary["task_failure_count"] = 1
            summary["recoverable"] = True
            self.db.finish_task(
                self.task_id,
                "failed",
                "E_TASK",
                summary,
                error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}",
            )
        finally:
            await self.client.close()

    async def _enumerate_pages(self, cursor: dict[str, Any]) -> None:
        page_no = int(cursor.get("next_page") or 1)
        while True:
            observation, contract = await self._observe_page(page_no)
            validate_cursor(observation, page_no)
            cursor = {
                "last_completed_page": page_no,
                "next_page": observation.next_page,
                "total_pages": observation.total_pages,
                "enumeration_complete": observation.next_page is None,
            }
            items = [
                {
                    "item_key": entry.item_key,
                    "business_id": entry.business_id,
                    "schema_version": entry.schema_version,
                    "revision": entry.revision,
                    "detail_url": urljoin(f"{self.archive_base_url}/", entry.detail_url),
                }
                for entry in observation.items
            ]
            self.db.save_page(self.task_id, page_no, cursor, items)
            if observation.next_page is None:
                return
            page_no = observation.next_page

    async def _observe_page(self, page_no: int) -> tuple[RenderObservation, ListContract]:
        page_url = (
            f"{self.archive_base_url}/archive/list?page={page_no}"
            f"&wave={self.wave}"
            f"&delay_profile={self.config['delay_profile']}"
            f"&fault_profile={self.config['fault_profile']}"
        )
        page_result = await self.client.fetch(page_url, kind="list_page")
        self.db.save_raw_response(
            task_id=self.task_id,
            session_id=self.client.sessions.crawl_session_id,
            item_key=None,
            url=page_result.url,
            kind="list_page",
            status_code=page_result.status_code,
            body=page_result.text,
            headers=page_result.headers,
            wave=self.wave,
            request_seq=page_result.request_seq,
        )
        contract = parse_list_contract(page_result.text)
        fragment_url = urljoin(page_result.url, contract.render_api)
        deadline = time.monotonic() + float(self.config["render_timeout_s"])
        max_observations = int(self.config["max_render_observations"])
        previous_count: int | None = None
        stable_count = 0
        observations = 0
        while True:
            if observations >= max_observations or time.monotonic() >= deadline:
                raise RenderContractError(
                    "E_RENDER_TIMEOUT",
                    "entry container did not reach two consecutive stable observations",
                    {
                        "page": page_no,
                        "observations": observations,
                        "last_item_count": previous_count,
                        "stable_count": stable_count,
                    },
                )
            result = await self.client.fetch(fragment_url, kind="list_fragment")
            observations += 1
            observation = parse_fragment(result.text, contract)
            if observation.container_present and observation.item_count == previous_count:
                stable_count += 1
            else:
                stable_count = 1 if observation.container_present else 0
            previous_count = observation.item_count
            observation.stable_count = stable_count
            snapshot = {
                "task_id": self.task_id,
                "page_no": page_no,
                "observed_at": utcnow(),
                "url": result.url,
                "container_present": observation.container_present,
                "item_count": observation.item_count,
                "stable_count": stable_count,
                "body_sha256": observation.body_sha256,
                "fragment_excerpt": observation.fragment_excerpt,
                "accepted": observation.container_present and stable_count >= contract.stable_observations,
            }
            self.db.record_render_snapshot(snapshot)
            if snapshot["accepted"]:
                self.db.save_raw_response(
                    task_id=self.task_id,
                    session_id=self.client.sessions.crawl_session_id,
                    item_key=None,
                    url=result.url,
                    kind="list_fragment_accepted",
                    status_code=result.status_code,
                    body=result.text,
                    headers=result.headers,
                    wave=self.wave,
                    request_seq=result.request_seq,
                )
                return observation, contract
            await asyncio.sleep(0)

    async def _process_items(self, items: list[dict[str, Any]]) -> None:
        semaphore = asyncio.Semaphore(int(self.config["concurrency"]))
        await self.client.ensure_session()

        async def process(item: dict[str, Any]) -> None:
            async with semaphore:
                await self._process_item(item)

        await asyncio.gather(*(process(item) for item in items), return_exceptions=False)

    async def _process_item(self, item: dict[str, Any]) -> None:
        attempts = 0
        try:
            result = await self.client.fetch(
                item["detail_url"], kind="detail", item_key=item["item_key"]
            )
            attempts = result.attempts
            try:
                record = parse_detail(
                    result.text,
                    fallback_business_id=item["business_id"],
                    fallback_schema_version=item["schema_version"],
                    fallback_revision=int(item["revision"]),
                    source_url=result.url,
                )
            except DetailParseError as exc:
                self.db.save_raw_response(
                    task_id=self.task_id,
                    session_id=self.client.sessions.crawl_session_id,
                    item_key=item["item_key"],
                    url=result.url,
                    kind="detail_parse_error",
                    status_code=result.status_code,
                    body=result.text,
                    headers=result.headers,
                    wave=self.wave,
                    request_seq=result.request_seq,
                )
                raise ItemError(exc.code, str(exc)) from exc
            self.db.ingest_detail(
                task_id=self.task_id,
                session_id=self.client.sessions.crawl_session_id,
                item_key=item["item_key"],
                url=result.url,
                body=result.text,
                headers=result.headers,
                wave=self.wave,
                request_seq=result.request_seq,
                record=record,
                attempts=attempts,
            )
        except CrawlError as exc:
            attempts = int(exc.details.get("attempts", attempts) or attempts)
            self.db.fail_item(
                self.task_id,
                item["item_key"],
                exc.code,
                str(exc),
                attempts,
            )
        except Exception as exc:
            self.db.fail_item(
                self.task_id,
                item["item_key"],
                "E_ITEM",
                f"{type(exc).__name__}: {exc}",
                attempts,
            )
