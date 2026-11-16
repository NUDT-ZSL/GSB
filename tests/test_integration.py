"""端到端：真实 mock 站点 + Playwright 渲染 + 采集器 + SQLite。"""
import asyncio

from app.collector import run_task
from app.mock_site import TOTAL_TICKETS
from app.store import Store

BASE_CFG = {
    "concurrency": 4,
    "max_retries": 3,
    "backoff_base_ms": 20,
    "render_timeout_ms": 8000,
    "session_replay_limit": 12,
    "source": "test",
}


def _new_task(store: Store, base_url: str, **overrides) -> int:
    cfg = {**BASE_CFG, "base_url": base_url, **overrides}
    tid = store.create_task(cfg)
    store.transition(tid, "running")
    return tid


def test_full_run_and_resume_no_duplicates(store, mock_site):
    mock_site.configure({"render_delay_ms": 100, "session_ttl_requests": 5,
                         "flaky_503": {"detail:TK-1003": 2}})
    tid = _new_task(store, mock_site.base_url)
    asyncio.run(run_task(store, tid))
    s = store.summary(tid)
    assert s["task"]["status"] == "succeeded"
    assert s["stored_items"] == TOTAL_TICKETS
    assert s["distinct_items"] == TOTAL_TICKETS
    assert s["checkpoints"] == TOTAL_TICKETS
    assert s["parse_status"] == {"ok": 16, "raw_fallback": 8}


def test_pause_then_resume_skips_confirmed(store, mock_site):
    mock_site.configure({"render_delay_ms": 100})
    tid = _new_task(store, mock_site.base_url)
    # 入库达到 6 条后协作式暂停
    asyncio.run(run_task(store, tid,
                         pause_check=lambda: store.summary(tid)["stored_items"] >= 6))
    s = store.summary(tid)
    assert s["task"]["status"] == "paused"
    assert 0 < s["stored_items"] < TOTAL_TICKETS
    # 恢复：已确认条目跳过，未确认重抓，幂等键保证无重复行
    store.transition(tid, "running", note="resume")
    asyncio.run(run_task(store, tid))
    s2 = store.summary(tid)
    assert s2["task"]["status"] == "succeeded"
    assert s2["stored_items"] == TOTAL_TICKETS
    assert s2["distinct_items"] == TOTAL_TICKETS
    assert s2["checkpoints"] == TOTAL_TICKETS


def test_item_level_503_failure_not_task_failure(store, mock_site):
    mock_site.configure({"render_delay_ms": 100, "flaky_503": {"detail:TK-1005": 99}})
    tid = _new_task(store, mock_site.base_url)
    asyncio.run(run_task(store, tid))
    s = store.summary(tid)
    assert s["task"]["status"] == "succeeded"  # 条目级失败不拖垮任务
    assert s["stored_items"] == TOTAL_TICKETS - 1
    assert s["item_failure_count"] == 1
    assert s["item_failures"][0]["ticket_id"] == "TK-1005"
    assert s["item_failures"][0]["code"] == "E_HTTP_503"


def test_session_exhaustion_pauses_task_resumable(store, mock_site):
    mock_site.configure({"render_delay_ms": 100, "session_fail_always": True})
    tid = _new_task(store, mock_site.base_url, session_replay_limit=3)
    asyncio.run(run_task(store, tid))
    s = store.summary(tid)
    assert s["task"]["status"] == "paused"
    assert s["task"]["error_code"] == "E_SESSION"
    # 过期响应未被当作正常数据入库
    assert s["stored_items"] == 0
    # 关闭开关后可恢复至成功
    mock_site.configure({"render_delay_ms": 100, "session_fail_always": False})
    store.transition(tid, "running", note="resume")
    asyncio.run(run_task(store, tid))
    assert store.summary(tid)["task"]["status"] == "succeeded"


def test_render_timeout_records_snapshot(store, mock_site):
    mock_site.configure({"render_delay_ms": 5000})
    tid = _new_task(store, mock_site.base_url, render_timeout_ms=100, max_retries=1)
    asyncio.run(run_task(store, tid))
    s = store.summary(tid)
    assert s["task"]["status"] == "failed"
    assert s["task"]["error_code"] == "E_RENDER_TIMEOUT"
    assert s["snapshots"] >= 1


def test_pause_check_cooperative(store, mock_site):
    mock_site.configure({"render_delay_ms": 100})
    tid = _new_task(store, mock_site.base_url)
    asyncio.run(run_task(store, tid, pause_check=lambda: True))
    assert store.get_task(tid)["status"] == "paused"
