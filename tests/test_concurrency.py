"""业务要求 8：多进程并发一致性与幂等提交。"""
import multiprocessing

import pytest

from reopt import errors
from reopt.errors import BusinessError
from reopt.runner import Runner


def _worker(state_dir, queue):
    try:
        runner = Runner.open(state_dir)
        try:
            runner.run()
        finally:
            runner.store.close()
        queue.put(("ok", None))
    except Exception as exc:  # noqa: BLE001
        queue.put(("error", repr(exc)))


def _snapshot(state_dir):
    runner = Runner.open(state_dir)
    try:
        store = runner.store
        return {
            "watermark": store.watermark(),
            "batches": store.committed_batches(),
            "observations": store.observations(),
            "triggers": store.triggers(),
            "switches": store.switches(),
            "form_version": store.form_version(),
        }
    finally:
        runner.store.close()


def test_concurrent_instances_commit_once(loaded_state):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(loaded_state, queue))
             for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=300)
    results = [queue.get(timeout=60) for _ in procs]
    assert all(status == "ok" for status, _ in results), results
    state = _snapshot(loaded_state)
    assert state["watermark"] == 6
    # 同一批次同一形态版本只提交一次
    keys = [(b["seq"], b["form_version"]) for b in state["batches"]]
    assert len(keys) == len(set(keys))
    # 形态指针只被整体替换：版本严格递增且无重复
    to_versions = [sw["to_version"] for sw in state["switches"]]
    assert len(to_versions) == len(set(to_versions))
    assert to_versions == sorted(to_versions)
    # 观测不重复
    obs_keys = [(o["seq"], o["step_idx"], o["form_version"])
                for o in state["observations"]]
    assert len(obs_keys) == len(set(obs_keys))


def test_idempotent_batch_commit(loaded_state):
    runner = Runner.open(loaded_state)
    store = runner.store
    try:
        with store.transaction() as conn:
            assert store.commit_batch(conn, 1, 1, "d", {"A": "x"}, {"A": 1},
                                      "r", 0) is True
        with store.transaction() as conn:
            # 相同内容重复提交：幂等
            assert store.commit_batch(conn, 1, 1, "d", {"A": "x"}, {"A": 1},
                                      "r", 0) is False
        with pytest.raises(BusinessError) as excinfo:
            with store.transaction() as conn:
                store.commit_batch(conn, 1, 1, "different", {"A": "x"},
                                   {"A": 1}, "r", 0)
        assert excinfo.value.code == errors.E_BATCH_CONFLICT
    finally:
        store.close()
