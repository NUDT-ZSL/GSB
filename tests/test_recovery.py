import os
import subprocess
import sys

from recovery_engine.clock import LogicalClock
from recovery_engine.engine import Engine
from recovery_engine.executor import SimulatedExecutor, SimulatedHealth
from recovery_engine.store import Store
from tests.conftest import V1_PATH, V2_DIR, V2_META

ROOT = os.path.join(os.path.dirname(__file__), "..")


def run_crashed_child(db_path, crash_after):
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "crash_child.py"),
         db_path, str(crash_after)],
        capture_output=True, text=True)
    assert proc.returncode != 0  # 被 SIGKILL 强杀


def clock_from_waterline(store):
    waterline = store.get_clock_waterline() or 10000
    return LogicalClock([waterline + i * 100 for i in range(400)], tolerance_ms=50)


def test_crash_recovery_replays_only_after_waterline(tmp_path):
    db_path = str(tmp_path / "state.db")
    run_crashed_child(db_path, crash_after=3)

    store = Store(db_path)
    committed = store.committed_actions()
    pending = store.pending_actions()
    # 子进程提交了 db-stop、db-switch 后在 api-stop 提交前被强杀
    assert [(c["service"], c["action_id"]) for c in committed] == [
        ("db", "db-stop"), ("db", "db-switch")]
    # 半提交动作被识别
    assert [(p["service"], p["action_id"]) for p in pending] == [("api", "api-stop-v2")]

    # 恢复：清除半提交，只重放水位之后的动作
    engine = Engine(store, clock_from_waterline(store),
                    SimulatedExecutor(), SimulatedHealth())
    plan = engine.build_plan(V1_PATH, V2_DIR, V2_META)
    report = engine.recover(plan)
    assert report["half_committed_discarded"] == [
        {"service": "api", "action_id": "api-stop-v2"}]
    assert report["resumed"]["api"]["from_waterline"] == -1
    assert store.pending_actions() == []
    # db 已提交动作绝不重放
    db_commits = store.committed_actions("db")
    assert len(db_commits) == 2

    # 恢复后继续完成剩余服务的切换：幂等，不重复提交
    engine.failover(plan)
    all_commits = store.committed_actions()
    keys = [(c["service"], c["action_id"], c["version"]) for c in all_commits]
    assert len(keys) == len(set(keys)) == 9
    assert store.get_service_state("web")["phase"] == "failed_over"


def test_crash_during_failback_recovers_at_stable_rung(tmp_path):
    db_path = str(tmp_path / "state.db")
    # 先正常完成切换
    store = Store(db_path)
    engine = Engine(store, LogicalClock([10000 + i * 100 for i in range(400)],
                                        tolerance_ms=50),
                    SimulatedExecutor(), SimulatedHealth())
    plan = engine.build_plan(V1_PATH, V2_DIR, V2_META)
    engine.failover(plan)
    # 回切中途“崩溃”：用仅前两级健康的结论源模拟停滞，验证持久化阶梯
    from recovery_engine.executor import SimulatedHealth as SH
    stalled = Engine(store, clock_from_waterline(store), SimulatedExecutor(),
                     SH(healthy_rungs={"db": 1}))
    stalled.failback(plan, services=["db"])
    assert store.get_service_state("db")["ladder_rung"] == 1
    # 恢复后续推：从已验证阶梯继续，最终到达 100%
    resumed = Engine(store, clock_from_waterline(store), SimulatedExecutor(),
                     SimulatedHealth())
    results = resumed.failback(plan, services=["db"])
    assert results["db"]["status"] == "done"
    assert store.get_service_state("db")["ladder_rung"] == 3
