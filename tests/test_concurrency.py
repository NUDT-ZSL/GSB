import multiprocessing
import os

from recovery_engine.store import Store
from tests.conftest import V1_PATH, V2_DIR, V2_META


def _worker(db_path, rounds, queue):
    from recovery_engine.clock import LogicalClock
    from recovery_engine.engine import Engine
    from recovery_engine.executor import SimulatedExecutor, SimulatedHealth
    store = Store(db_path)
    # 并发场景聚焦幂等提交：各实例时钟独立，放宽水位容差
    # （时钟回拨语义由 tests/test_clock.py 专项覆盖）
    clock = LogicalClock([10000 + i * 100 for i in range(2000)],
                         tolerance_ms=10 ** 9)
    engine = Engine(store, clock, SimulatedExecutor(), SimulatedHealth())
    plan = engine.build_plan(V1_PATH, V2_DIR, V2_META)
    for _ in range(rounds):
        engine.failover(plan)
    queue.put(len(engine.executor.executed))


def test_concurrent_failover_commits_once_and_idempotent(tmp_path):
    db_path = str(tmp_path / "shared.db")
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(db_path, 3, queue))
             for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    assert all(p.exitcode == 0 for p in procs)

    store = Store(db_path)
    commits = store.committed_actions()
    keys = [(c["service"], c["action_id"], c["version"]) for c in commits]
    # 两个编排实例并行处理同一批服务：同一动作版本只被提交一次
    assert len(keys) == len(set(keys)) == 9
    # 每个服务最终都处于完整已提交阶段
    for name in ("db", "api", "cache", "legacy", "tie", "web"):
        assert store.get_service_state(name)["phase"] == "failed_over"
    # 读取方只看到完整已提交阶段：无半提交残留
    assert store.pending_actions() == []


def test_concurrent_plan_switch_serialized(tmp_path):
    import json
    from recovery_engine.packages import apply_full_package
    from tests.test_packages import make_package

    db_path = str(tmp_path / "plans.db")
    Store(db_path)  # 初始化 schema
    pkg = make_package(tmp_path, version=1)

    def switch():
        store = Store(db_path)
        apply_full_package(store, pkg, plan_json=json.dumps({"v": 1}))

    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=switch) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    # 文件锁保护下并发整体替换串行化；版本冲突者以业务错误退出
    assert any(p.exitcode == 0 for p in procs)
    assert Store(db_path).get_plan_pointer() == 1
