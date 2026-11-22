"""需求 8：多进程并发一致性与异常链路隔离。"""
import hashlib
import multiprocessing as mp

import pytest

from chunkmeta.errors import (
    E_BUDGET_EXHAUSTED,
    E_CLOCK_REGRESSION,
    StoreError,
)
from chunkmeta.service import StoreService

from .conftest import make_bucket, make_config, make_manifest


def _worker(args):
    root_dir, worker_id, rounds = args
    config = make_config()
    svc = StoreService(root_dir, config)
    try:
        shared = make_manifest(key="shared/obj.bin", version=1, chunk_count=3)
        unique = make_manifest(key=f"unique/obj-{worker_id}.bin", version=1,
                               chunk_count=2)
        bucket = make_bucket(clock_ms=None)  # 并发提交不携带外部时钟，纯步进
        results = []
        for _ in range(rounds):
            svc.ingest(shared, bucket)
            svc.ingest(unique, bucket)
            data = svc.read("media-store", "shared/obj.bin").data
            results.append(hashlib.sha256(data).hexdigest())
        return worker_id, results
    finally:
        svc.close()


def test_concurrent_writers_single_commit(tmp_path):
    root = tmp_path / "store"
    workers, rounds = 4, 3
    ctx = mp.get_context("fork")
    with ctx.Pool(workers) as pool:
        outcomes = pool.map(_worker, [(str(root), i, rounds) for i in range(workers)])
    svc = StoreService(root, make_config())
    try:
        # 同一对象同一内容版本只被提交一次
        count = svc.db.conn.execute(
            "SELECT COUNT(*) FROM objects WHERE object_key='shared/obj.bin'"
        ).fetchone()[0]
        assert count == 1
        # 同一分片摘要只被物化一次：文件数 == 唯一摘要数
        unique_digests = {c.sha256 for c in make_manifest(
            key="shared/obj.bin", version=1, chunk_count=3).chunks}
        for i in range(workers):
            unique_digests |= {c.sha256 for c in make_manifest(
                key=f"unique/obj-{i}.bin", version=1, chunk_count=2).chunks}
        assert svc.chunks.count_files() == len(unique_digests)
        # 全部读取结果一致
        expect = svc.read("media-store", "shared/obj.bin").data
        for _wid, digests in outcomes:
            assert set(digests) == {hashlib.sha256(expect).hexdigest()}
    finally:
        svc.close()


def test_clock_regression_maps_to_error(service):
    service.ingest(make_manifest(), make_bucket(clock_ms=1000))
    stale = make_bucket(clock_ms=100)  # 回拨 900ms > 阈值 100ms
    with pytest.raises(StoreError) as exc:
        service.ingest(make_manifest(key="docs/c.txt"), stale)
    assert exc.value.code == E_CLOCK_REGRESSION
    assert exc.value.details["provided_ms"] == 100
    assert exc.value.details["watermark"] >= 1000


def test_budget_window_exhaustion(service):
    service.ingest(make_manifest(), make_bucket())
    with service.db.tx():
        clock = service.db.tick_clock()
        # 窗口容量 = 预算 * window_factor；占满后再次预留必须拒绝
        for _ in range(service.config.budget_window_factor):
            service.db.reserve_budget(clock, 50000, 50000 * 4)
        with pytest.raises(StoreError) as exc:
            service.db.reserve_budget(clock, 50000, 50000 * 4)
        assert exc.value.code == E_BUDGET_EXHAUSTED
        assert exc.value.details["window"] == clock
