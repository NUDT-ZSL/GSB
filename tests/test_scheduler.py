import asyncio

from app.config import SchedulerConfig
from app.crawler.scheduler import AdaptiveScheduler


def make(**kw):
    return AdaptiveScheduler(SchedulerConfig(**kw))


def test_shrink_on_rate_limited_and_floor():
    s = make(initial_inflight=8, max_inflight=8, decrease_factor=0.5, min_inflight=2)
    s.observe("rate_limited", 10)
    assert s.limit == 4
    s.observe("server_error", 10)
    assert s.limit == 2
    s.observe("timeout", -1)
    assert s.limit == 2  # hard floor


def test_bounded_regrowth_and_hard_cap():
    s = make(initial_inflight=1, max_inflight=3, increase_step=1, increase_after=2)
    for _ in range(2):
        s.observe("success", 10)
    assert s.limit == 2
    for _ in range(4):
        s.observe("success", 10)
    assert s.limit == 3
    for _ in range(10):
        s.observe("success", 10)
    assert s.limit == 3  # hard cap


def test_latency_pressure_shrinks():
    s = make(initial_inflight=4, max_inflight=4, latency_high_ms=100, latency_window=4)
    s.observe("success", 500)
    assert s.limit == 2  # 4 * 0.5
    for _ in range(3):
        s.observe("success", 500)
    assert s.limit == 1  # hard floor


def test_retry_queue_pressure_blocks_growth():
    s = make(initial_inflight=1, max_inflight=5, increase_after=1, retry_queue_max=4)
    s._retry_waiting = 3  # > retry_queue_max // 2
    s.observe("success", 10)
    assert s.limit == 1


def test_acquire_respects_dynamic_limit():
    async def scenario():
        s = make(initial_inflight=1, max_inflight=4)
        await s.acquire()
        acquired = False

        async def second():
            nonlocal acquired
            await s.acquire()
            acquired = True

        task = asyncio.create_task(second())
        await asyncio.sleep(0.05)
        assert not acquired  # queued, not admitted
        s._limit = 2
        await s.release()
        await asyncio.wait_for(task, 1)
        assert acquired
    asyncio.run(scenario())


def test_retry_waiters_bounded_by_semaphore():
    async def scenario():
        s = make(retry_queue_max=2, backoff_base_ms=200, backoff_max_ms=200)
        await asyncio.gather(*(s.retry_wait(1) for _ in range(6)))
        assert s.retry_waiting == 0
    asyncio.run(scenario())


def test_snapshot_restore_roundtrip():
    s = make(initial_inflight=6, max_inflight=8, min_inflight=1)
    s.observe("rate_limited", 5)
    snap = s.snapshot()
    restored = make(initial_inflight=8, max_inflight=8)
    restored.restore(snap)
    assert restored.limit == s.limit
